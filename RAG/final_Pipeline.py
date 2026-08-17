import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from RAG.rag import add_pdf, query


def final_pipeline(pdf_path: str, question: str, k: int = 4) -> list[str]:
    """Ingest a PDF, then retrieve the chunks most relevant to a question."""
    with open(pdf_path, "rb") as f:
        file_bytes = f.read()

    num_chunks = add_pdf(file_bytes, filename=Path(pdf_path).name)
    print(f"Number of chunks added to the collection: {num_chunks}")

    results = query(question, k=k)
    print(f"--- Top {len(results)} result(s) for: {question!r} ---")
    for i, text in enumerate(results, 1):
        print(f"[{i}] {text[:200]}")

    return results


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python final_Pipeline.py <path-to-pdf> <question>")
        sys.exit(1)

    final_pipeline(sys.argv[1], sys.argv[2])
