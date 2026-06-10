"""
setup_embeddings.py — Embed anomaly_history + create Atlas Vector Search index.

Uses fastembed (BAAI/bge-small-en-v1.5, 384 dims, fully local, no API quota).
Embeds all documents in anomaly_history on the 'description' field.
Creates the 'anomaly_semantic' Atlas vector search index.

Run once before demo:
    cd backend && python -m scripts.setup_embeddings
"""
import sys, time, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("setup_embeddings")

EMBED_DIM = 384
INDEX_NAME = "anomaly_semantic"
FIELD = "embedding"


def main():
    from db import app_db
    from fastembed import TextEmbedding

    # ── Step 1: Check how many docs need embedding ────────────────────────────
    total = app_db.anomaly_history.count_documents({})
    needs_embed = app_db.anomaly_history.count_documents({FIELD: {"$exists": False}})
    logger.info("anomaly_history: %d total, %d need embedding", total, needs_embed)

    if needs_embed == 0:
        logger.info("All documents already have embeddings.")
    else:
        # ── Step 2: Load embedding model (downloads ~40MB on first run) ───────
        logger.info("Loading BAAI/bge-small-en-v1.5 (%d dims, local, no quota)...", EMBED_DIM)
        model = TextEmbedding("BAAI/bge-small-en-v1.5")

        # ── Step 3: Fetch docs without embedding ─────────────────────────────
        docs = list(app_db.anomaly_history.find(
            {FIELD: {"$exists": False}},
            {"description": 1, "anomaly_type": 1, "collection_name": 1}
        ))
        logger.info("Embedding %d documents...", len(docs))

        # Batch embed (fastembed batches internally for efficiency)
        texts = []
        for d in docs:
            text = d.get("description", "")
            if not text:
                text = f"{d.get('anomaly_type','')} in {d.get('collection_name','')}"
            texts.append(text)

        t0 = time.monotonic()
        embeddings = list(model.embed(texts))
        embed_ms = (time.monotonic() - t0) * 1000
        logger.info("Embedded %d docs in %.0fms (%.1f ms/doc)", len(embeddings), embed_ms, embed_ms / len(embeddings))

        # ── Step 4: Write embeddings back to MongoDB ──────────────────────────
        t0 = time.monotonic()
        from pymongo import UpdateOne
        ops = [
            UpdateOne(
                {"_id": doc["_id"]},
                {"$set": {FIELD: emb.tolist()}}
            )
            for doc, emb in zip(docs, embeddings)
        ]
        result = app_db.anomaly_history.bulk_write(ops, ordered=False)
        write_ms = (time.monotonic() - t0) * 1000
        logger.info("Wrote %d embeddings in %.0fms", result.modified_count, write_ms)

    # ── Step 5: Create Atlas Vector Search index ──────────────────────────────
    # pymongo 4.7+ supports create_search_index() for Atlas Search/VectorSearch
    logger.info("Creating Atlas Vector Search index '%s'...", INDEX_NAME)
    try:
        # Check if index already exists
        existing = list(app_db.anomaly_history.list_search_indexes())
        existing_names = [ix.get("name") for ix in existing]
        logger.info("Existing search indexes: %s", existing_names)

        if INDEX_NAME in existing_names:
            logger.info("Index '%s' already exists — skipping creation.", INDEX_NAME)
        else:
            idx_model = {
                "name": INDEX_NAME,
                "type": "vectorSearch",
                "definition": {
                    "fields": [
                        {
                            "type":          "vector",
                            "path":          FIELD,
                            "numDimensions": EMBED_DIM,
                            "similarity":    "cosine",
                        }
                    ]
                },
            }
            app_db.anomaly_history.create_search_index(idx_model)
            logger.info("Index creation request sent. Atlas will build it in ~1-2 minutes.")
            logger.info("Check status: Atlas UI → Search Indexes → anomaly_semantic → ACTIVE")

    except Exception as e:
        if "already exists" in str(e).lower():
            logger.info("Index already exists.")
        else:
            logger.warning("Index creation failed: %s", e)
            logger.info("")
            logger.info("MANUAL FALLBACK — create index in Atlas UI:")
            logger.info("1. Atlas → Browse Collections → querysentinel.anomaly_history")
            logger.info("2. Search Indexes tab → Create Search Index")
            logger.info("3. Type: Vector Search, Name: anomaly_semantic")
            logger.info("4. JSON definition:")
            logger.info(json.dumps({"fields": [{"type": "vector", "path": FIELD, "numDimensions": EMBED_DIM, "similarity": "cosine"}]}, indent=2))

    # ── Step 6: Verify ────────────────────────────────────────────────────────
    sample = app_db.anomaly_history.find_one({FIELD: {"$exists": True}})
    if sample:
        emb = sample.get(FIELD, [])
        logger.info("Verification: found document with embedding, dims=%d", len(emb))
    else:
        logger.error("No documents with embeddings found after setup!")

    # ── Update similar.py to use correct numDimensions ────────────────────────
    # The agent instruction mentions path: embedding — that's correct.
    # We just need to confirm the index name matches: anomaly_semantic ✓

    logger.info("")
    logger.info("=" * 60)
    logger.info("SETUP COMPLETE")
    logger.info("  Embedding model: BAAI/bge-small-en-v1.5 (%d dims)", EMBED_DIM)
    logger.info("  Index name:      %s", INDEX_NAME)
    logger.info("  Field:           %s", FIELD)
    logger.info("  Collection:      querysentinel.anomaly_history")
    logger.info("")
    logger.info("Atlas takes 1-3 minutes to build the index.")
    logger.info("Run again to check progress, or check Atlas UI → Search Indexes.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
