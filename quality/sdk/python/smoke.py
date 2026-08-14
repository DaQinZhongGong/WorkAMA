from __future__ import annotations

import json
import os

from openai import OpenAI


client = OpenAI(
    api_key=os.environ["WORKAMA_API_KEY"],
    base_url=os.getenv("WORKAMA_BASE_URL", "http://gateway:8080/v1"),
    timeout=20,
    max_retries=0,
)

models = client.models.list()
completion = client.chat.completions.create(
    model="workama-chat",
    messages=[{"role": "user", "content": "Python SDK compatibility probe"}],
)
stream = client.chat.completions.create(
    model="workama-chat",
    messages=[{"role": "user", "content": "Python streaming probe"}],
    stream=True,
)
stream_text = "".join(chunk.choices[0].delta.content or "" for chunk in stream)
embedding = client.embeddings.create(
    model="workama-embed", input="Python embedding probe"
)

assert any(model.id == "workama-chat" for model in models.data)
assert completion.choices[0].message.content
assert stream_text
assert len(embedding.data[0].embedding) == 16

print(
    json.dumps(
        {
            "sdk": "openai-python",
            "version": "2.45.0",
            "models": len(models.data),
            "completion": True,
            "streaming": True,
            "embedding_dimensions": len(embedding.data[0].embedding),
        }
    )
)
