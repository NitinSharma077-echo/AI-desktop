import tempfile
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


def load_pdf(file_path: str) -> list[Document]:
    """Load a PDF from disk into one Document per page."""
    loader = PyPDFLoader(file_path)
    return loader.load()


def chunk_documents(
    documents: list[Document],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> list[Document]:
    """Split documents into smaller overlapping chunks for embedding/retrieval."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_documents(documents)


def process_uploaded_pdf(
    file_bytes: bytes,
    filename: str = "upload.pdf",
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> list[Document]:
    """Take raw PDF bytes (e.g. from a Streamlit file uploader), load and chunk them."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / filename
        tmp_path.write_bytes(file_bytes)
        documents = load_pdf(str(tmp_path))
    return chunk_documents(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python file.py <path-to-pdf>")
        sys.exit(1)

    docs = load_pdf(sys.argv[1])
    chunks = chunk_documents(docs)
    print(f"Loaded {len(docs)} page(s), split into {len(chunks)} chunk(s).")
    if chunks:
        print("--- First chunk preview ---")
        print(chunks[0].page_content[:300])
