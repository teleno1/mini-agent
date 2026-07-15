# OpenAI-compatible Provider subset

Mini Agent's production Provider adapter is `OpenAICompatibleModelProvider` in
`mini_agent.providers.openai_compatible`. It uses direct `httpx` transport and
posts to `<base_url>/chat/completions` with `stream: true` and usage
collection. A Base URL that already ends in `/chat/completions` is accepted as
an endpoint.

The supported response stream is Server-Sent Events containing one Chat
Completions choice. The adapter normalizes the response ID, assistant text,
function Tool Call IDs and argument fragments, prompt/completion usage, and
`stop` or `tool_calls` into Mini Agent Stream Events. It accepts standard
`data:` and `[DONE]` records and rejects unknown SSE fields, multiple choices,
changed IDs, invalid JSON, unsupported stop reasons, incomplete Tool Calls,
and malformed usage.

Structured Tools are sent as the Chat Completions `tools` array. Tool schemas
come from the Context Frame or the adapter's static definitions and are
translated to `{type: "function", function: {name, description, parameters}}`.
The adapter records successful Tool Call chunks as detected support and marks
support unavailable when a Provider rejects the structured-tools request.

Default request bounds are 10 seconds to connect, 60 seconds to receive the
first event, 60 seconds of stream idleness, and 10 minutes total. Pre-stream
rate-limit, connection, timeout, and HTTP 5xx failures may be retried at most
twice; a partially emitted response is never retried automatically. Provider
keys are sent only in the Authorization header and are omitted from failure
descriptions.
