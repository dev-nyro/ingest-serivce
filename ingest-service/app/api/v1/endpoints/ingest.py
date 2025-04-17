# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io
import asyncio
from milvus_haystack import MilvusDocumentStore

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query, Request, Response
)
from minio.error import S3Error

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.tasks.process_document import process_document_haystack_task
from app.services.minio_client import MinioStorageClient

log = structlog.get_logger(__name__)

router = APIRouter()

# Helper para obtención dinámica de estado en Milvus
async def get_milvus_chunk_count(document_id: uuid.UUID) -> int:
    """Cuenta los chunks indexados en Milvus para un documento específico."""
    loop = asyncio.get_running_loop()
    def _count_chunks():
        # Inicializar conexión con Milvus
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Recuperar todos los documentos que coincidan con este document_id
        docs = store.get_all_documents(filters={"document_id": str(document_id)})
        return len(docs or [])
    try:
        return await loop.run_in_executor(None, _count_chunks)
    except Exception:
        return 0

# --- Dependencias CORREGIDAS para usar X- Headers ---
# Estas dependencias AHORA son la fuente de verdad para ID de compañía y usuario

async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    """Obtiene y valida el Company ID de la cabecera X-Company-ID."""
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        # Usar 422 ya que el gateway DEBERÍA haberla añadido. Si falta, es un error de procesamiento/configuración.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    """Obtiene y valida el User ID de la cabecera X-User-ID."""
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# Opcional: obtener email si se necesita
# async def get_current_user_email(x_user_email: Optional[str] = Header(None, alias="X-User-Email")) -> Optional[str]:
#    return x_user_email

# --- Endpoints Modificados para USAR las nuevas dependencias ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento via API Gateway. Usa X-Company-ID y X-User-ID.",
)
async def ingest_document_haystack(
    # *** CORRECCIÓN CLAVE: Usar las dependencias definidas arriba ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    # Ya no se depende de Authorization
    # --- Parámetros del Form ---
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales"),
    file: UploadFile = File(..., description="El archivo a ingestar"),
    request: Request = None # Para obtener request_id opcionalmente
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    request_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id), # Ya no es opcional aquí
        filename=file.filename,
        content_type=file.content_type
    )
    request_log.info("Processing document ingestion request from gateway")

    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported file type received", received_type=content_type)
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: '{content_type}'. Supported: {settings.SUPPORTED_CONTENT_TYPES}")
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict): raise ValueError("Metadata is not a JSON object")
    except (json.JSONDecodeError, ValueError) as json_err:
        request_log.warning("Invalid metadata JSON received", error=str(json_err))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON format for metadata: {json_err}")

    # PREVENCIÓN DE DUPLICADOS: Buscar documento existente por company_id y file_name en estado != error
    existing_docs = await postgres_client.list_documents_by_company(company_id, limit=1000, offset=0)
    for doc in existing_docs:
        if doc["file_name"] == file.filename and doc["status"] != DocumentStatus.ERROR.value:
            request_log.warning("Intento de subida duplicada detectado", filename=file.filename, status=doc["status"])
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un documento con el mismo nombre en estado '{doc['status']}'. Elimina o reintenta el anterior antes de subir uno nuevo.")

    minio_client = None; minio_object_name = None; document_id = None; task_id = None
    try:
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=content_type,
            metadata=metadata
            # user_id=user_id, # Descomentar si la tabla `documents` tiene y requiere `user_id`
        )
        request_log = request_log.bind(document_id=str(document_id))

        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0:
            await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty.")
            request_log.warning("Uploaded file is empty")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file cannot be empty.")

        file_stream = io.BytesIO(file_content)
        minio_client = MinioStorageClient()
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)

        # Pasar IDs como string a la tarea Celery
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            # user_id_str=str(user_id), # Pasar si la tarea lo necesita
            minio_object_name=minio_object_name,
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata
        )
        task_id = task.id
        request_log.info("Haystack processing task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document received and queued.")

    except HTTPException as http_exc: raise http_exc
    except (IOError, S3Error) as storage_err:
        request_log.error("Storage error during upload", error=str(storage_err))
        if document_id: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {storage_err}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service error.")
    except Exception as e:
        request_log.exception("Unexpected error during document ingestion")
        if document_id: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Ingestion error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error during ingestion.")
    finally:
        if file: await file.close()

@router.get(
    "/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado de procesamiento de un documento específico usando su ID.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    status_log = log.bind(request_id=request_id, document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    try:
        doc_data = await postgres_client.get_document_status(document_id)
        if not doc_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        # Validar pertenencia a la compañía
        if doc_data.get("company_id") != company_id:
            status_log.warning("Attempt to access document status from another company", owner_company=str(doc_data.get('company_id')))
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        response_data = schemas.StatusResponse.model_validate(doc_data)
        # Verificar existencia real en MinIO
        file_path = doc_data.get("file_path")
        if file_path:
            try:
                minio_client = MinioStorageClient()
                exists = await minio_client.file_exists(file_path)
            except Exception:
                exists = False
        else:
            exists = False
        # Contar chunks en Milvus
        milvus_count = await get_milvus_chunk_count(document_id)
        # Asignar campos adicionales
        response_data.minio_exists = exists
        response_data.milvus_chunk_count = milvus_count
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            DocumentStatus.INDEXED: "Document processed and indexed.", # Añadir si se usa este estado
            DocumentStatus.ERROR: f"Processing error: {response_data.error_message or 'Unknown error'}",
        }
        response_data.message = status_messages.get(response_data.status, "Unknown status.")
        status_log.info("Returning document status", status=response_data.status)
        return response_data
    except HTTPException as http_exc: raise http_exc
    except Exception as e: status_log.exception("Error retrieving status"); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve document status.")

@router.get(
    "/status",
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista paginada de documentos y sus estados para la compañía.",
)
async def list_ingestion_statuses(
    # *** CORRECCIÓN CLAVE: Usar la dependencia correcta ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Ya no se depende de Authorization
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        response_list = []
        for doc in documents_data:
            # Mark processed if DB chunk_count > 0
            current = DocumentStatus(doc.get("status"))
            count = doc.get("chunk_count") or 0
            if current in (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING) and count > 0:
                # Actualiza estado a PROCESSED si ya tiene chunks
                await postgres_client.update_document_status(
                    document_id=doc["id"],
                    status=DocumentStatus.PROCESSED,
                    chunk_count=count
                )
            # Verify MinIO existence, mark error if missing
            file_path = doc.get("file_path")
            try:
                exists = await MinioStorageClient().file_exists(file_path) if file_path else False
            except Exception:
                exists = False
            if not exists and current not in (DocumentStatus.ERROR,):
                await postgres_client.update_document_status(
                    document_id=doc["id"],
                    status=DocumentStatus.ERROR,
                    error_message="File missing in MinIO"
                )
            # Obtener conteo real de Milvus para lista
            try:
                milvus_count = await get_milvus_chunk_count(doc["id"])
            except Exception:
                milvus_count = None
            doc["milvus_chunk_count"] = milvus_count
            # Append enriched response
            doc["minio_exists"] = exists
            response_list.append(schemas.StatusResponse.model_validate(doc))
        list_log.info(f"Returning status list for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing document statuses.")

@router.post(
    "/retry/{document_id}",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Reintentar la ingesta de un documento con error",
    description="Permite reintentar la ingesta de un documento que falló. Solo disponible si el estado es 'error'."
)
async def retry_document_ingest(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    retry_log = log.bind(request_id=request_id, company_id=str(company_id), user_id=str(user_id), document_id=str(document_id))
    # 1. Buscar el documento y validar estado
    doc = await postgres_client.get_document_status(document_id)
    if not doc or doc.get("company_id") != str(company_id):
        retry_log.warning("Documento no encontrado o no pertenece a la compañía")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    if doc["status"] != DocumentStatus.ERROR.value:
        retry_log.warning("Solo se puede reintentar si el estado es 'error'", current_status=doc["status"])
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se puede reintentar la ingesta si el estado es 'error'.")
    # 2. Reencolar la tarea Celery
    task = process_document_haystack_task.delay(
        document_id_str=str(document_id),
        company_id_str=str(company_id),
        minio_object_name=doc["file_path"],
        file_name=doc["file_name"],
        content_type=doc["file_type"],
        original_metadata=doc.get("metadata", {})
    )
    retry_log.info("Reintento de ingesta encolado", task_id=task.id)
    # 3. Actualizar estado a 'processing'
    await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.PROCESSING, error_message=None)
    return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING, message="Reintento de ingesta encolado correctamente.")

@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un documento",
    description="Elimina un documento y su registro de la BD."
)
async def delete_document_endpoint(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id)
):
    delete_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    # Validar existencia y pertenencia
    doc = await postgres_client.get_document_status(document_id)
    if not doc or doc.get("company_id") != str(company_id):
        delete_log.warning("Documento no encontrado o no pertenece a la compañía")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    # Eliminar registro
    deleted = await postgres_client.delete_document(document_id)
    if not deleted:
        delete_log.error("Fallo al eliminar documento en la BD")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error eliminando documento.")
    delete_log.info("Documento eliminado exitosamente")
    return Response(status_code=status.HTTP_204_NO_CONTENT)