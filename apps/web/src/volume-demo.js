// Explicit, session-only usability fixture. Never enters assessment evidence or exports.
const topics = [
  ["raw", "Закупки", "цены и условия поставки", "закупочную цену и запас"],
  [
    "finance",
    "Финансирование",
    "условия кредитования",
    "тип ставки и сроки договора",
  ],
  [
    "packaging",
    "Покупатели",
    "планы производства покупателей",
    "состав клиентов и их заказы",
  ],
  ["logistics", "Логистика", "сроки доставки", "маршрут и дату своей поставки"],
  [
    "demand",
    "Потребление",
    "потребительский спрос",
    "посещаемость и средний чек",
  ],
];
export function volumeSources(count = 150) {
  return Array.from({ length: count }, (_, i) => {
    const [topic, label, subject, check] = topics[i % topics.length];
    const n = Math.floor(i / topics.length) + 1;
    const day = String(22 - (Math.floor(i / 10) % 22)).padStart(2, "0");
    const kind = ["Наблюдение", "Прогноз автора", "Мнение"][
      Math.floor(i / 5) % 3
    ];
    return {
      id: "volume-" + i,
      eventId: "volume-event-" + i,
      title: `${label}: ${subject} · пример ${n}`,
      text: `Учебный материал № ${i + 1} для проверки интерфейса на большом потоке. Тема — ${subject}. ${kind === "Наблюдение" ? "В условной выборке наблюдалось изменение показателя." : "Автор предполагает изменение показателя; это не установленный факт."} Для проверки связи с компанией нужно сопоставить ${check}. Эта публикация вымышлена и не участвует в оценках компании.`,
      publisher: "Демонстрационное издание " + ((i % 6) + 1),
      author: "Учебный автор",
      publishedAt: `2026-10-${day}`,
      eventAt: "",
      applies: [topic],
      kind,
      synthetic: true,
      available: true,
      status: "ready",
      excluded: false,
      url: "",
    };
  });
}
