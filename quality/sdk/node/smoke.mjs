import OpenAI from "openai";

const client = new OpenAI({
  apiKey: process.env.WORKAMA_API_KEY,
  baseURL: process.env.WORKAMA_BASE_URL ?? "http://gateway:8080/v1",
  timeout: 20_000,
  maxRetries: 0,
});

const models = await client.models.list();
const completion = await client.chat.completions.create({
  model: "workama-chat",
  messages: [{ role: "user", content: "Node SDK compatibility probe" }],
});
const stream = await client.chat.completions.create({
  model: "workama-chat",
  messages: [{ role: "user", content: "Node streaming probe" }],
  stream: true,
});
let streamText = "";
for await (const chunk of stream) {
  streamText += chunk.choices[0]?.delta?.content ?? "";
}
const embedding = await client.embeddings.create({
  model: "workama-embed",
  input: "Node embedding probe",
});
const embeddingVector = embedding.data[0]?.embedding;

if (!models.data.some((model) => model.id === "workama-chat")) {
  throw new Error("workama-chat was not returned by models.list");
}
if (!completion.choices[0]?.message?.content || !streamText) {
  throw new Error("chat completion or streaming response was empty");
}
if (embeddingVector?.length !== 16) {
  console.error(
    JSON.stringify({
      embeddingType: typeof embeddingVector,
      isArray: Array.isArray(embeddingVector),
      embeddingLength: embeddingVector?.length,
      embeddingValue: embeddingVector,
    }),
  );
  throw new Error("embedding response did not have 16 dimensions");
}

console.log(
  JSON.stringify({
    sdk: "openai-node",
    version: "6.46.0",
    models: models.data.length,
    completion: true,
    streaming: true,
    embeddingDimensions: embeddingVector.length,
  }),
);
