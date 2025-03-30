# ./app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever # Integración correcta
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore connection."""
    # Loguear los parámetros que SÍ se usarán
    log.debug("Initializing MilvusDocumentStore for Query Service",
             connection_uri=str(settings.MILVUS_URI), # Loguear la URI que se pasará
             collection=settings.MILVUS_COLLECTION_NAME,
             embedding_field=settings.MILVUS_EMBEDDING_FIELD,
             content_field=settings.MILVUS_CONTENT_FIELD,
             metadata_fields=settings.MILVUS_METADATA_FIELDS,
             search_params=settings.MILVUS_SEARCH_PARAMS)
    try:
        # *** CORRECCIÓN: Usar connection_args para la URI ***
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)}, # Pasar la URI dentro de connection_args
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # dim no suele ser necesario al conectar a una colección existente
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS, # Campos a recuperar de Milvus
            search_params=settings.MILVUS_SEARCH_PARAMS,    # Parámetros para la búsqueda
            consistency_level="Strong",                     # Nivel de consistencia
            # index_params no se necesitan aquí, solo para escritura/creación
        )
        log.info("MilvusDocumentStore initialized successfully")
        return store
    except Exception as e:
        # Loguear el error específico de inicialización
        log.error("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        # Lanzar un error claro para que el startup/health check falle correctamente
        raise RuntimeError(f"Could not initialize Milvus Document Store: {e}") from e


def get_openai_text_embedder() -> OpenAITextEmbedder:
    """Initializes the OpenAI Embedder for text (queries)."""
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY")
    if not api_key_secret.resolve_value():
         log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")
         # Podríamos lanzar un error si la clave es crítica para la funcionalidad básica
         # raise ValueError("Missing OpenAI API Key for text embedder")

    return OpenAITextEmbedder(
        api_key=api_key_secret,
        model=settings.OPENAI_EMBEDDING_MODEL,
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    """Initializes the MilvusEmbeddingRetriever."""
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(
        document_store=document_store,
        # top_k y filters se pasarán dinámicamente en pipeline.run()
    )

def get_prompt_builder() -> PromptBuilder:
    """Initializes the PromptBuilder with the RAG template."""
    log.debug("Initializing PromptBuilder", template_preview=settings.RAG_PROMPT_TEMPLATE[:100] + "...")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction ---

_rag_pipeline_instance: Optional[Pipeline] = None

def build_rag_pipeline() -> Pipeline:
    """
    Builds the Haystack RAG pipeline by initializing and connecting components.
    Caches the pipeline instance globally after the first successful build.
    """
    global _rag_pipeline_instance
    # Devolver instancia cacheada si ya existe
    if _rag_pipeline_instance:
        log.debug("Returning existing RAG pipeline instance.")
        return _rag_pipeline_instance

    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()

    try:
        # 1. Initialize components (ahora get_milvus_document_store debería funcionar)
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        # 2. Add components to the pipeline
        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        # 3. Connect components
        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline # Guardar instancia global
        return rag_pipeline

    except Exception as e:
        # Loguear el error detallado si falla la construcción
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        # Lanzar para que el startup falle si no se puede construir el pipeline
        raise RuntimeError("Could not build the RAG pipeline") from e


# --- Pipeline Execution ---

async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Runs the RAG pipeline for a given query and company_id.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A")
    run_log.info("Running RAG pipeline...")

    # Obtener o construir el pipeline (ahora debería funcionar en startup si Milvus está ok)
    try:
        pipeline = build_rag_pipeline()
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         # Si no se puede construir el pipeline, no se puede procesar la query
         raise HTTPException(status_code=503, detail="RAG pipeline is not available.")


    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    # Usar el campo correcto definido en settings para el filtro
    retriever_filters = {settings.MILVUS_COMPANY_ID_FIELD: company_id}
    run_log.debug("Retriever filters prepared", filters=retriever_filters, top_k=retriever_top_k)

    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    run_log.debug("Pipeline input prepared", input_data=pipeline_input)

    try:
        # Ejecutar Haystack pipeline en thread
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"])
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        if "prompt" in prompt_builder_output:
             prompt_data = prompt_builder_output["prompt"]
             if isinstance(prompt_data, list):
                 text_parts = [msg.content for msg in prompt_data if hasattr(msg, 'content') and isinstance(msg.content, str)]
                 generated_prompt = "\n".join(text_parts)
             elif isinstance(prompt_data, str):
                  generated_prompt = prompt_data
             else:
                  run_log.warning("Unexpected prompt format from prompt_builder", prompt_type=type(prompt_data))
                  generated_prompt = str(prompt_data)

        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever.")
            if not generated_prompt:
                 generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."

        if not generated_prompt:
             run_log.error("Failed to extract or generate prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")

        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        # Llamar a Gemini
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        # Loguear interacción
        log_id: Optional[uuid.UUID] = None
        try:
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None]
            user_uuid = uuid.UUID(user_id) if user_id else None
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id),
                user_id=user_uuid,
                query=query,
                response=answer,
                retrieved_doc_ids=doc_ids,
                retrieved_doc_scores=doc_scores,
                metadata={"retriever_top_k": retriever_top_k}
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)

        return answer, retrieved_docs, log_id

    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        # Relanzar una excepción más genérica o específica según el caso
        raise HTTPException(status_code=500, detail=f"Error processing query: {type(e).__name__}")

# Función para verificar dependencias (llamada desde health check)
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks critical dependencies for the pipeline (e.g., Milvus)."""
    results = {"milvus_connection": "pending"}
    try:
        # Intentar obtener el DocumentStore como prueba de conexión/configuración
        # Esto ahora debería funcionar o lanzar un error claro si Milvus no está accesible
        store = get_milvus_document_store()
        # Realizar una operación ligera para confirmar la conexión
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful (count documents)", count=count)
    except Exception as e:
        # Capturar cualquier excepción durante la inicialización o la operación
        error_msg = f"{type(e).__name__}: {str(e)}"
        results["milvus_connection"] = f"error: {error_msg[:100]}" # Limitar longitud
        log.warning("Milvus dependency check failed", error=error_msg, exc_info=False)
    return results