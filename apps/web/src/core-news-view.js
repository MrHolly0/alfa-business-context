export function resolveCoreNewsView(id, events = []) {
  if (!id) return { kind: "list" };
  const event = events.find((item) => item.id === id);
  return event ? { kind: "detail", event } : { kind: "missing" };
}
