# ./app/services/minio_client.py (CORREGIDO - download_file_stream ahora es sync)
import io
import uuid
from typing import IO, BinaryIO # Usar BinaryIO para type hint
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio # Import asyncio para run_in_executor

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO."""

    def __init__(self):
        # La inicialización sigue siendo síncrona
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=settings.MINIO_BUCKET_NAME)
        except Exception as e:
            log.error("Failed to initialize MinIO client", error=str(e), exc_info=True)
            raise

    def _ensure_bucket_exists(self):
        """Crea el bucket si no existe (síncrono)."""
        try:
            found = self.client.bucket_exists(settings.MINIO_BUCKET_NAME)
            if not found:
                self.client.make_bucket(settings.MINIO_BUCKET_NAME)
                log.info(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' created.")
            else:
                log.debug(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{settings.MINIO_BUCKET_NAME}'", error=str(e), exc_info=True)
            raise

    # upload_file puede permanecer async si la llamada a put_object se hace en executor
    # o si se usa un cliente MinIO asíncrono en el futuro. Por ahora, lo dejamos async
    # asumiendo que la tarea Celery lo llamará desde run_in_executor si es necesario.
    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes], # Acepta cualquier stream de bytes
        content_type: str,
        content_length: int
    ) -> str:
        """
        Sube un archivo a MinIO de forma asíncrona (ejecutando la operación síncrona en un executor).
        Retorna el nombre del objeto en MinIO (object_name).
        """
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor...")

        loop = asyncio.get_running_loop()
        try:
            # Ejecutar la operación síncrona de MinIO en un executor
            result = await loop.run_in_executor(
                None, # Usa el ThreadPoolExecutor por defecto
                lambda: self.client.put_object(
                    settings.MINIO_BUCKET_NAME,
                    object_name,
                    file_content_stream, # Pasar el stream directamente
                    length=content_length,
                    content_type=content_type,
                )
            )
            upload_log.info("File uploaded successfully to MinIO via executor", etag=result.etag, version_id=result.version_id)
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), exc_info=True)
            raise # Re-raise the specific S3Error
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            raise # Re-raise generic exceptions


    # *** CORREGIDO: Hacerla síncrona para llamarla desde run_in_executor ***
    def download_file_stream_sync(
        self,
        object_name: str
    ) -> io.BytesIO:
        """
        Descarga un archivo de MinIO como un stream en memoria (BytesIO).
        Esta es una operación SÍNCRONA.
        """
        download_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync)...")
        response = None
        try:
            # Operación bloqueante de red/IO
            response = self.client.get_object(settings.MINIO_BUCKET_NAME, object_name)
            file_data = response.read() # Leer todo el contenido (bloqueante)
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0) # Reset stream position
            return file_stream
        except S3Error as e:
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), exc_info=True)
            # Es importante lanzar una excepción clara si el archivo no se encuentra
            if e.code == 'NoSuchKey':
                 raise FileNotFoundError(f"Object not found in MinIO: {object_name}") from e
            else:
                 # Otro error de S3
                 raise IOError(f"S3 error downloading file {object_name}: {e.code}") from e
        except Exception as e:
             # Capturar otros posibles errores
             download_log.error("Unexpected error during sync file download", error=str(e), exc_info=True)
             raise IOError(f"Unexpected error downloading file {object_name}") from e
        finally:
            # Asegurar que la conexión se libera siempre
            if response:
                response.close()
                response.release_conn()

    # Mantenemos la versión async como wrapper por si se necesita en otros lados,
    # pero ahora llama a la versión síncrona en el executor.
    async def download_file_stream(
        self,
        object_name: str
    ) -> io.BytesIO:
        """
        Descarga un archivo de MinIO como un stream en memoria (BytesIO) de forma asíncrona.
        Ejecuta la descarga síncrona en un executor.
        """
        download_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor...")
        loop = asyncio.get_running_loop()
        try:
            file_stream = await loop.run_in_executor(
                None, # Usa el ThreadPoolExecutor por defecto
                self.download_file_stream_sync, # Llama a la función síncrona
                object_name
            )
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError: # Capturar el error específico de archivo no encontrado
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise # Relanzar FileNotFoundError
        except Exception as e:
            download_log.error("Error downloading file via executor", error=str(e), exc_info=True)
            raise # Relanzar otras excepciones