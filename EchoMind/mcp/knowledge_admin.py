"""Explicit local maintenance; run only while the application writer is stopped."""
import argparse
import json
import os

from .knowledge_base import KnowledgeBase


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stats", "migrate-legacy", "delete-quarantined"])
    parser.add_argument("--organization-id", default="public")
    parser.add_argument("--document-id")
    args = parser.parse_args()
    kb = KnowledgeBase(chroma_host=os.getenv("CHROMA_HOST", "localhost"),
                       chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
                       allow_local_fallback=False, seed_defaults=False)
    if args.command == "migrate-legacy":
        result = {"migrated_chunks": kb.migrate_legacy_documents(args.organization_id)}
    elif args.command == "delete-quarantined":
        if not args.document_id:
            parser.error("--document-id is required")
        quarantined = kb.stats()["quarantined_documents"]
        if args.document_id not in quarantined:
            parser.error("document is not quarantined; use the authenticated API for normal deletion")
        result = {"deleted_chunks": kb.delete_document(args.document_id)}
    else:
        result = kb.stats()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
