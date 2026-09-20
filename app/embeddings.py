"""
Lightweight embeddings for SupportRAG (Project 2).

Why TF-IDF + SVD instead of a neural embedding model (e.g. sentence
transformers): those models pull in a multi-hundred-MB deep learning
stack (torch + model weights). For a small, fixed knowledge base like
this one, TF-IDF (word-frequency vectors) reduced with SVD (Latent
Semantic Analysis) gets you meaningful semantic similarity - it can
match "VPN will not connect" to a ticket about "cannot reach the office
network" via shared/related vocabulary - at a fraction of the footprint
and with zero external model downloads or API calls.

This is a genuine engineering tradeoff, not a corner cut silently: it's
documented here and in the README. The interface below (`fit`, `embed`)
is intentionally the only thing the rest of the app depends on, so
swapping this out for real neural embeddings later (e.g. sentence-
transformers, OpenAI/Anthropic embeddings, or a local ONNX model) is a
change to this one file only - nothing in supportrag.py or main.py
would need to change.
"""
from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD

# Kept low relative to the KB size (currently 16 articles) - SVD needs
# n_components < number of documents. If the KB grows well past ~100
# articles, this can be raised (try 50-100) for finer-grained similarity.
N_COMPONENTS = 8


class Embedder:
    """
    Fits a TF-IDF + SVD pipeline once on a fixed corpus, then embeds new
    text into that same vector space. Not safe to call `embed()` before
    `fit()`, and re-calling `fit()` invalidates any previously computed
    embeddings (they'd no longer be in the same space) - the service
    layer (supportrag.py) is responsible for fitting once and re-using.
    """

    def __init__(self) -> None:
        self._vectorizer = TfidfVectorizer(stop_words="english", max_df=0.85)
        self._svd = TruncatedSVD(n_components=N_COMPONENTS, random_state=42)
        self._fitted = False

    def fit(self, corpus: list[str]) -> None:
        tfidf_matrix = self._vectorizer.fit_transform(corpus)
        n_components = min(N_COMPONENTS, tfidf_matrix.shape[1] - 1, tfidf_matrix.shape[0] - 1)
        if n_components < 1:
            raise ValueError("Corpus too small to fit an embedding space.")
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._svd.fit(tfidf_matrix)
        self._fitted = True

    def embed(self, text: str) -> list[float]:
        if not self._fitted:
            raise RuntimeError("Embedder.fit() must be called before embed().")
        tfidf_vec = self._vectorizer.transform([text])
        dense_vec = self._svd.transform(tfidf_vec)[0]
        return dense_vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Plain cosine similarity, computed in Python/numpy.

    On real Postgres + pgvector, this is where you'd instead run
    `ORDER BY embedding <=> query_embedding` and let the database do it
    with an index - see the note in models.py and database.py. Doing it
    in Python here keeps the project runnable on plain SQLite with zero
    extra infrastructure, which matters more for a portfolio project
    than query performance does.
    """
    vec_a = np.array(a)
    vec_b = np.array(b)
    denom = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if denom == 0:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)
