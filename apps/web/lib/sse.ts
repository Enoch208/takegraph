export type SseMessage = {
  id: number;
  event: string;
  data: unknown;
};

export async function consumeBuildEvents(options: {
  url: string;
  token: string;
  cursor: number;
  signal: AbortSignal;
  onMessage: (message: SseMessage) => void;
  onHeartbeat?: () => void;
}): Promise<void> {
  const response = await fetch(options.url, {
    headers: {
      accept: "text/event-stream",
      authorization: `Bearer ${options.token}`,
      "Last-Event-ID": String(options.cursor),
    },
    cache: "no-store",
    signal: options.signal,
  });
  if (!response.ok) {
    throw new Error(`SSE ${response.status} ${response.statusText}`);
  }
  if (!response.body) {
    throw new Error("SSE response body was empty.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let id = options.cursor;
  let event = "message";
  let dataLines: string[] = [];

  const flush = () => {
    if (dataLines.length === 0) {
      event = "message";
      return;
    }
    const raw = dataLines.join("\n");
    dataLines = [];
    try {
      options.onMessage({ id, event, data: JSON.parse(raw) as unknown });
    } catch {
      options.onMessage({ id, event, data: raw });
    }
    event = "message";
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      flush();
      return;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n");
    buffer = parts.pop() ?? "";
    for (const line of parts) {
      if (line === "") {
        flush();
        continue;
      }
      if (line.startsWith(":")) {
        options.onHeartbeat?.();
        continue;
      }
      if (line.startsWith("id:")) {
        const parsed = Number.parseInt(line.slice(3).trim(), 10);
        if (Number.isFinite(parsed)) {
          id = parsed;
        }
        continue;
      }
      if (line.startsWith("event:")) {
        event = line.slice(6).trim();
        continue;
      }
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
  }
}
