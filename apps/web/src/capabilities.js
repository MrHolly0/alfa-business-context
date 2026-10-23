export function backendCapabilities(status) {
  const declared = status?.capabilities || {};
  const capabilities = {
    news: declared.news === true,
    import: declared.import === true,
    analyze: declared.analyze === true,
  };
  return {
    ...capabilities,
    llm: status?.llm === true && capabilities.analyze,
  };
}
