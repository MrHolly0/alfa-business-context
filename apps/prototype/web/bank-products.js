// Commercial placement is separate from assessment scoring and evidence.
export const products = {
  credit: {
    id: "business-credit",
    title: "Финансирование для бизнеса",
    art: "finance",
    description: "Посмотрите варианты финансирования и условия на сайте банка.",
    url: "https://alfabank.ru/sme/profits/businesscredit/",
  },
  counterparty: {
    id: "counterparty-check",
    title: "Проверка контрагента",
    art: "opportunity",
    description:
      "Перед первой сделкой можно посмотреть отчёт о новом покупателе.",
    url: "https://alfabank.ru/sme/abp/services/check-contragent/",
  },
};
export function contextualProduct(assessment, state, now = Date.now()) {
  if (
    state.bankOffers === false ||
    !assessment.sources?.length ||
    assessment.score < 30 ||
    state.hidden?.includes(assessment.id) ||
    state.actions?.[assessment.id]?.status === "done"
  )
    return null;
  const facts = state.company.facts;
  let product;
  if (assessment.id === "finance" && facts.plannedBorrowing === "yes") {
    product = {
      ...products.credit,
      reason: "Вы указали, что планируете новое заимствование.",
    };
  }
  if (assessment.id === "opportunity" && facts.freeCapacity !== "no") {
    product = {
      ...products.counterparty,
      reason: "В этой ситуации вы рассматриваете работу с новым покупателем.",
    };
  }
  if (!product) return null;
  const hiddenAt = state.dismissedOffers?.[product.id];
  return hiddenAt && now - hiddenAt < 30 * 86400000 ? null : product;
}
