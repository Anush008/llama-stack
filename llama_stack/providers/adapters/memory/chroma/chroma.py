# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

import json
from typing import List
from urllib.parse import urlparse

import chromadb
from numpy.typing import NDArray

from llama_stack.apis.memory import *  # noqa: F403

from llama_stack.providers.utils.memory.vector_store import (
    BankWithIndex,
    EmbeddingIndex,
)


class ChromaIndex(EmbeddingIndex):
    def __init__(self, client: chromadb.AsyncHttpClient, collection):
        self.client = client
        self.collection = collection

    async def add_chunks(self, chunks: List[Chunk], embeddings: NDArray):
        assert len(chunks) == len(
            embeddings
        ), f"Chunk length {len(chunks)} does not match embedding length {len(embeddings)}"

        await self.collection.add(
            documents=[chunk.json() for chunk in chunks],
            embeddings=embeddings,
            ids=[f"{c.document_id}:chunk-{i}" for i, c in enumerate(chunks)],
        )

    async def query(self, embedding: NDArray, k: int) -> QueryDocumentsResponse:
        results = await self.collection.query(
            query_embeddings=[embedding.tolist()],
            n_results=k,
            include=["documents", "distances"],
        )
        distances = results["distances"][0]
        documents = results["documents"][0]

        chunks = []
        scores = []
        for dist, doc in zip(distances, documents):
            try:
                doc = json.loads(doc)
                chunk = Chunk(**doc)
            except Exception:
                import traceback

                traceback.print_exc()
                print(f"Failed to parse document: {doc}")
                continue

            chunks.append(chunk)
            scores.append(1.0 / float(dist))

        return QueryDocumentsResponse(chunks=chunks, scores=scores)


class ChromaMemoryAdapter(Memory):
    def __init__(self, url: str) -> None:
        print(f"Initializing ChromaMemoryAdapter with url: {url}")
        url = url.rstrip("/")
        parsed = urlparse(url)

        if parsed.path and parsed.path != "/":
            raise ValueError("URL should not contain a path")

        self.host = parsed.hostname
        self.port = parsed.port

        self.client = None
        self.cache = {}

    async def initialize(self) -> None:
        try:
            print(f"Connecting to Chroma server at: {self.host}:{self.port}")
            self.client = await chromadb.AsyncHttpClient(host=self.host, port=self.port)
        except Exception as e:
            import traceback

            traceback.print_exc()
            raise RuntimeError("Could not connect to Chroma server") from e

    async def shutdown(self) -> None:
        pass

    async def register_memory_bank(
        self,
        memory_bank: MemoryBankDef,
    ) -> None:
        assert (
            memory_bank.type == MemoryBankType.vector.value
        ), f"Only vector banks are supported {memory_bank.type}"

        collection = await self.client.create_collection(
            name=memory_bank.identifier,
        )
        bank_index = BankWithIndex(
            bank=memory_bank, index=ChromaIndex(self.client, collection)
        )
        self.cache[memory_bank.identifier] = bank_index

    async def _get_and_cache_bank_index(self, bank_id: str) -> Optional[BankWithIndex]:
        if bank_id in self.cache:
            return self.cache[bank_id]

        bank = await self.memory_bank_store.get_memory_bank(bank_id)
        if bank is None:
            raise ValueError(f"Bank {bank_id} not found")

        collections = await self.client.list_collections()
        for collection in collections:
            if collection.name == bank_id:
                index = BankWithIndex(
                    bank=bank,
                    index=ChromaIndex(self.client, collection),
                )
                self.cache[bank_id] = index
                return index

        return None

    async def insert_documents(
        self,
        bank_id: str,
        documents: List[MemoryBankDocument],
        ttl_seconds: Optional[int] = None,
    ) -> None:
        index = await self._get_and_cache_bank_index(bank_id)
        if not index:
            raise ValueError(f"Bank {bank_id} not found")

        await index.insert_documents(documents)

    async def query_documents(
        self,
        bank_id: str,
        query: InterleavedTextMedia,
        params: Optional[Dict[str, Any]] = None,
    ) -> QueryDocumentsResponse:
        index = await self._get_and_cache_bank_index(bank_id)
        if not index:
            raise ValueError(f"Bank {bank_id} not found")

        return await index.query_documents(query, params)
