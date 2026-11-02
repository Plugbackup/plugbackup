import { NBSP, readJson } from './common.mjs';

export const loadPlans = () => readJson('data', 'plans.json');

// Display strings for one plan. Numbers stay glued to their units.
export function planView(plan) {
  const free = plan.price_rub === 0;
  const access = plan.access ?? '';
  return {
    id: plan.id,
    name: plan.name,
    gb: String(plan.gb),
    size: `${plan.gb}${NBSP}ГБ`,
    free,
    price: free ? 'Бесплатно' : `${plan.price_rub}${NBSP}₽/мес`,
    priceValue: free ? 'Бесплатно' : `${plan.price_rub}${NBSP}₽`,
    period: free ? '' : '/мес',
    access,
    note: plan.note ?? '',
    line: plan.note ?? (access ? access[0].toUpperCase() + access.slice(1) : ''),
  };
}

export function planContext(data) {
  const plans = Object.fromEntries(data.plans.map((plan) => [plan.id, planView(plan)]));
  return {
    plans,
    bot: data.bot,
    site: data.site,
    payment: data.payment,
    payment_note: data.payment_note,
  };
}
