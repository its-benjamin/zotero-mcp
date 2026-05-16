from zotero_mcp.chroma_client import HuggingFaceEmbeddingFunction


def test_e5_huggingface_adds_query_and_passage_prefixes():
    calls = []

    class FakeModel:
        max_seq_length = 512
        tokenizer = None

        def encode(self, texts, convert_to_numpy=True, normalize_embeddings=False):
            calls.append((texts, normalize_embeddings))

            class FakeEmbeddings:
                def tolist(self):
                    return [[0.1, 0.2] for _ in texts]

            return FakeEmbeddings()

    ef = HuggingFaceEmbeddingFunction.__new__(HuggingFaceEmbeddingFunction)
    ef.model_name = "intfloat/multilingual-e5-small"
    ef.model = FakeModel()
    ef.max_input_tokens = 512
    ef._query_prefix = "query: "
    ef._document_prefix = "passage: "
    ef._normalize_embeddings = True

    assert [list(vec) for vec in ef(["dokumen"])] == [[0.1, 0.2]]
    assert ef.embed_query("hukum indonesia") == [0.1, 0.2]
    assert calls == [
        (["passage: dokumen"], True),
        (["query: hukum indonesia"], True),
    ]
