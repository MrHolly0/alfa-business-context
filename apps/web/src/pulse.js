// Business Pulse presentation. Metric values are explicit demo fixtures.
export function createPulse({ E, C, esc, I, B, state, root, persist, navigate, render }) {
  let period = 'all';
  let customRange = { start: '2026-10-01', end: '2026-10-22' };
  const definitions = [
    ['revenue','Выручка','sales','₽','Доход от продаж; новости о спросе и бюджетах клиентов.','trend'],
    ['margin','Операционная маржа','procurement','%','Доля операционной прибыли в выручке; влияние цен и затрат.','package'],
    ['cash','Денежный поток','finance','₽','Поступления за вычетом выплат за период.','card'],
    ['debt','Долговая нагрузка','finance','×','Чистый долг / EBITDA; чувствительность к условиям финансирования.','building'],
    ['receivables','Срок оплаты клиентами','sales','дней','Средний срок погашения дебиторской задолженности (DSO).','clock'],
    ['costs','Операционные расходы','procurement','₽','Расходы на ведение деятельности: персонал, услуги, аренда, технологии и закупки.','arrows'],
  ];
  const labels = { all:'Общая картина', week:'7 дней', month:'30 дней', quarter:'Квартал', custom:'Свой период' };
  function metrics() { return [...definitions.map(([id,name,domain,unit,description,icon])=>({id,name,domain,unit,description,icon})), ...(state().pulseMetrics || [])]; }
  function range() {
    const latest = E.activeSources(state()).map(s=>s.publishedAt).filter(Boolean).sort().at(-1) || E.TODAY;
    if(period==='all') return {start:'',end:latest};
    if(period==='custom') return customRange;
    const end = new Date(latest+'T12:00:00Z');
    end.setUTCDate(end.getUTCDate()-({week:7,month:30,quarter:90}[period]-1));
    return {start:end.toISOString().slice(0,10),end:latest};
  }
  function events(domain) { const r=range(); return E.events(state()).filter(e=>(!domain || C.inDomain(e,domain)) && (!r.start || e.date>=r.start) && e.date<=r.end); }
  function coreStatusLabel(status) {
    return ({ no_analysis:"Сводка пока не готова", partial:"Нужны уточнения", ready:"Готово", unknown:"Не установлено", observed:"Есть наблюдение", confirmed:"Подтверждено", proposed:"Предложено", blocked:"Проверка ограничена", error:"Не удалось проверить", published:"Опубликовано", failed:"Не удалось проверить", no_data:"Данных нет", not_evaluated:"Не проверено", unavailable:"Недоступно" })[status] || (status ? "Нужны уточнения" : "Статус не указан");
  }
  function coreDirectionLabel(direction) {
    return ({ up:"Рост", down:"Снижение", mixed:"Смешанное влияние", unknown:"Не определено", neutral:"Без направления" })[direction] || "Не определено";
  }
  function coreEventKind(kind) {
    return ({ source_document:"Новость", manual:"Добавлен вручную", official:"Официальное сообщение", forecast:"Прогноз", opinion:"Мнение" })[kind] || "Материал";
  }
  function coreExcerpt(value, limit = 280) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text;
  }
  function bar() { return `<div class="pulse-period" aria-label="Период обзора">${Object.entries(labels).map(([id,label])=>`<button type="button" data-action="pulse-period:${id}" aria-pressed="${period===id}">${label}</button>`).join('')}</div>`; }
  function sidebar() {
    const disabled=(label,icon,newItem=false)=>`<button class="navitem bank-disabled" disabled title="Недоступно в демонстрации">${I(icon)}<span>${label}</span>${newItem?'<small class="bank-new">НОВОЕ</small>':''}</button>`;
    return `<a class="brand" href="#home"><span>Альфа Банк</span></a><nav class="banknav" aria-label="Меню банка">${disabled('Новый платёж','plus')}${disabled('Платежи в работе','refresh')}${disabled('Реестры платежей','file')}${disabled('Выписка','file')}${disabled('Счета','card')}${disabled('Контрагенты','people')}<div class="navlabel">Мои сервисы</div><a class="navitem pulse-nav active" href="#home"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M1 12v5m22-5v5M9 16h6"/><circle cx="8.5" cy="12" r="1"/><circle cx="15.5" cy="12" r="1"/></svg><span>Бизнес-пульс</span><small class="bank-new">AI</small></a>${disabled('Аккредитивы','building')}${disabled('Альфа-Безопасность','lock',true)}${disabled('Заказ наличных','pay')}${disabled('Справки','file')}${disabled('Карты','card')}${disabled('Смена данных','file',true)}${disabled('Счета Эскроу','building',true)}</nav><div class="pulse-sidebar-footer"><span>Все сервисы и продукты</span><span>Бонусы и привилегии</span></div>`;
  }
  function dashboard() {
    const s=state(), r=range(), all=E.list(s), selected=events();
    const cases=all.filter(a=>a.sources.some(source=>selected.some(e=>e.sources.some(x=>x.id===source.id))));
    const values=s.company.sector==='restaurant'?['8,4 млн','16,2','+1,2 млн','1,4','12','7,0 млн']:s.company.sector==='software'?['36,5 млн','24,1','+4,8 млн','0,7','38','27,7 млн']:['1,32 млрд','18,4','+186 млн','2,1','46','1,08 млрд'];
    return `<div class="pulse-dashboard"><div class="pulse-heading"><div><span class="pulse-kicker">АНАЛИТИКА ВАШЕГО БИЗНЕСА</span><h1>Бизнес-пульс <span class="pulse-ai">AI</span></h1></div>${B('Как это работает','pulse-help','secondary','chat')}</div><section class="pulse-summary"><div class="pulse-summary-top"><span class="pulse-label">${I('context')} Анализ периода</span>${bar()}</div><div class="pulse-summary-body"><div><span class="pulse-kicker">${esc(labels[period])} · ${r.start?esc(r.start)+' — ': 'материалы до '}${esc(r.end)}</span><h2>${selected.length?'Общая оценка бизнеса':'За этот период материалов нет'}</h2><p>${selected.length?'Проверьте, как изменения спроса, расходов и условий финансирования могут повлиять на результаты компании. Выберите показатель ниже, чтобы изучить основания и обсудить следующие действия.':'Попробуйте расширить период. Отсутствие публикаций не означает отсутствие рисков.'}</p><div class="pulse-summary-links"><a href="#research-new">Подробный анализ периода ${I('right')}</a><button data-action="assistant">Обсудить с помощником ${I('chat')}</button></div></div><div class="pulse-summary-count"><strong>${selected.length}</strong><span>событий в материалах</span><small>Обзор по сохранённым данным</small></div></div></section><div class="pulse-section-title"><div><h2>Показатели бизнеса</h2><p>Выберите показатель, чтобы увидеть связанные новости</p></div>${B('Добавить метрику','pulse-add','secondary','plus')}</div><div class="pulse-metrics">${metrics().map((m,i)=>`<a class="pulse-metric" href="#metric/${encodeURIComponent(m.id)}"><div class="pulse-metric-top"><span>${esc(m.name)}</span>${I(m.icon||'trend')}</div><strong>${i<6?values[i]:'—'} <small>${esc(m.unit)}</small></strong><p>${i<6?'Демо · последний отчётный месяц':'Ваш показатель · данные не подключены'}</p><div class="pulse-metric-foot"><span>Связанных событий: ${events(m.domain).length}</span>${I('right')}</div></a>`).join('')}</div><p class="pulse-note">Значения показателей — пример, не данные счетов. Выбор периода меняет подборку материалов; значения метрик показаны за последний отчётный месяц.</p><section class="pulse-bottom-grid"><div class="pulse-panel"><div class="pulse-section-title"><h2>Требует внимания</h2><a href="#situations/all">Все ситуации →</a></div>${cases.slice(0,3).map(a=>`<a class="pulse-case" href="#detail/${a.id}"><span class="pulse-case-icon">${I(a.icon)}</span><span><strong>${esc(({packaging:"Выручка",raw:"Операционные расходы",finance:"Долговая нагрузка",software:"Выручка",demand:"Выручка",logistics:"Операционные расходы",opportunity:"Рост выручки"})[a.id] || a.metric)}</strong><small>${esc(a.next)}</small></span>${I('right')}</a>`).join('') || '<p>Для выбранного периода нет связанных ситуаций.</p>'}</div><div class="pulse-panel pulse-balance"><span class="pulse-kicker">ДОСТУПНЫЙ ОСТАТОК · ДЕМО</span><h2>${s.company.sector==='restaurant'?'3 840 000':'248 600 000'} <small>₽</small></h2><p>По всем рублёвым счетам</p><hr><span>Валютные счета</span><strong>0,00 $</strong><button disabled>Сформировать выписку · недоступно</button></div></section></div>`;
  }
  function coreDashboard(company, dashboard) {
    const metrics = dashboard?.metrics || [];
    const events = dashboard?.events || [];
    const metricValue = (metric) => {
      const observation = metric.observation;
      return observation && metric.status === "observed"
        ? `${esc(observation.value_raw)} <small>${esc(observation.unit || metric.unit || "")}</small>`
        : `— <small>${esc(metric.unit || "")}</small>`;
    };
    const summaryStatus = dashboard?.summary?.status;
    const summaryTitle = ["no_analysis", "partial"].includes(summaryStatus) ? "Сводка по компании ещё не готова" : "Сводка по компании";
    return `<div class="pulse-dashboard"><div class="pulse-heading"><div><span class="pulse-kicker">ДАННЫЕ КОМПАНИИ</span><h1>Бизнес-пульс</h1><p>${esc(company?.name || company?.synthetic_u || "")}</p></div></div><section class="pulse-summary"><div class="pulse-summary-body"><div><span class="pulse-kicker">Обзор компании</span><h2>${summaryTitle}</h2><p>${dashboard?.summary?.text ? esc(dashboard.summary.text) : "Показаны сохранённые показатели и события. Отсутствие данных не означает отсутствие риска."}</p></div><div class="pulse-summary-count"><strong>${events.length}</strong><span>событий</span><small>${metrics.length} показателей</small></div></div></section><div class="pulse-section-title"><div><h2>Показатели бизнеса</h2><p>Значения показаны только если они сохранены.</p></div>${B("Добавить показатель","nav:metric-new","secondary","plus")}</div><div class="pulse-metrics">${metrics.map((metric) => `<a class="pulse-metric" href="#metric/${encodeURIComponent(metric.id)}"><div class="pulse-metric-top"><span>${esc(metric.title)}</span>${I("trend")}</div><strong>${metricValue(metric)}</strong><p>${metric.status === "observed" ? `Источник: ${esc(metric.observation.source_label || "не указан")}` : "Нет данных"}</p><div class="pulse-metric-foot"><span>${metric.observation ? `Период до ${esc(metric.observation.period_end)}` : "Наблюдение не передавалось"}</span>${I("right")}</div></a>`).join("") || '<p class="hint">Показатели не созданы.</p>'}</div><p class="pulse-note">${esc(coreStatusLabel(summaryStatus))}. Сводка не подтверждает причинную связь.</p><section class="pulse-panel"><div class="pulse-section-title"><div><h2>События компании</h2><p>Сохранённые материалы</p></div><a href="#events">Все материалы →</a></div>${events.length ? events.map((event) => `<article class="pulse-news"><div><span class="pulse-kicker">${esc(coreEventKind(event.kind))} · ${esc(event.published_on)}</span><h3><a href="#news/${encodeURIComponent(event.id)}">${esc(event.title)}</a></h3><p>${esc(coreExcerpt(event.assessment_summary || event.summary || "Оценка не опубликована."))}</p></div>${event.assessment_status ? `<span class="badge">${esc(coreStatusLabel(event.assessment_status))}</span>` : ""}</article>`).join("") : '<p>Событий пока нет.</p>'}</section></div>`;
  }
  function coreMetricPage(metric, linkedEvents = [], canWrite = false) {
    if (!metric) return '<h1>Показатель не найден</h1><a href="#home">К обзору</a>';
    const o = metric.observation;
    const observationForm = canWrite ? `<section class="pulse-panel"><h2>Добавить наблюдение</h2><form id="core-observation-form" data-metric-id="${esc(metric.id)}" class="form-grid"><label class="field"><span>Значение</span><input name="value" required inputmode="decimal" maxlength="100" pattern="-?[0-9]+([.][0-9]+)?"></label><label class="field"><span>Единица</span><input name="unit" value="${esc(metric.unit || "")}" maxlength="40"></label><label class="field"><span>Начало периода</span><input name="periodStart" type="date" required></label><label class="field"><span>Конец периода</span><input name="periodEnd" type="date" required></label><label class="field full"><span>Источник значения</span><input name="source" required maxlength="200"></label><div class="actions full"><button type="submit" class="btn primary">Сохранить наблюдение</button></div></form></section>` : "";
    return `<a href="#home" class="inline-link">← К обзору</a><div class="pulse-heading"><div><span class="pulse-kicker">ДАННЫЕ КОМПАНИИ</span><h1>${esc(metric.title)}</h1><p>${esc(metric.description || "")}</p></div></div><section class="pulse-panel"><h2>Последнее наблюдение</h2>${o ? `<dl class="profile-facts"><div><dt>Значение</dt><dd>${esc(o.value_raw)} ${esc(o.unit || metric.unit || "")}</dd></div><div><dt>Период</dt><dd>${esc(o.period_start)} — ${esc(o.period_end)}</dd></div><div><dt>Источник</dt><dd>${esc(o.source_label || "Не указан")}</dd></div></dl>` : '<p>Данных за период нет. Демонстрационные значения не подставляются.</p>'}</section>${observationForm}<section class="pulse-panel"><h2>Связанные события</h2>${linkedEvents.length ? linkedEvents.map((event) => `<article class="pulse-news"><div><span class="pulse-kicker">${esc(coreEventKind(event.kind))} · ${esc(event.published_on || "Дата не указана")}</span><h3><a href="#news/${encodeURIComponent(event.id)}">${esc(event.title)}</a></h3><p>${esc(event.explanation || event.assessment?.summary || event.summary || "Оценка не опубликована.")}</p><p class="hint">Связь: ${esc(coreDirectionLabel(event.direction))} · ${esc(coreStatusLabel(event.status))}${event.assessment?.status ? ` · Оценка: ${esc(coreStatusLabel(event.assessment.status))}` : ""}</p></div></article>`).join("") : '<p>Сохранённых связей событий с этим показателем нет.</p>'}</section>`;
  }
  function metricPage(id) {
    const m=metrics().find(m=>m.id===id); if(!m) return '<h1>Метрика не найдена</h1><a href="#home">К показателям</a>';
    const rows=events(m.domain);
    return `<a href="#home" class="inline-link">← К показателям</a><div class="pulse-heading"><div><span class="pulse-kicker">ПОКАЗАТЕЛЬ БИЗНЕСА</span><h1>${esc(m.name)}</h1><p>${esc(m.description)}</p></div>${B('Как это работает','pulse-help','secondary','chat')}</div>${bar()}<p class="pulse-note">Материалы по теме «${esc(C.domains.find(d=>d.id===m.domain)?.label||m.domain)}». Связь тематическая; влияние на значение показателя ещё нужно подтвердить.</p><section class="pulse-panel"><h2>Связанные новости · ${rows.length}</h2>${rows.map(e=>`<article class="pulse-news"><div><span class="pulse-kicker">${esc(e.kind)} · ${esc(e.date)}</span><h3><a href="#source/${encodeURIComponent(e.sources[0].id)}">${esc(e.title)}</a></h3><p>${esc(e.text.slice(0,220))}</p></div>${B('Обсудить','ask-source:'+e.sources[0].id,'secondary','chat')}</article>`).join('') || '<p>Подходящих материалов за этот период пока нет. Выберите другой период или добавьте материал через «Управление».</p>'}</section><div class="actions"><a class="btn secondary" href="#situations/all">Связанные рабочие ситуации</a><a class="btn secondary" href="#sources">Управлять источниками</a></div>`;
  }
  function dialog(title,body,onSubmit) {
    document.getElementById('pulse-dialog')?.remove();
    const before=document.activeElement;
    const d=document.createElement('dialog'); d.id='pulse-dialog';d.className='pulse-dialog';d.setAttribute('aria-labelledby','pulse-dialog-title');
    d.innerHTML=`<form><button type="button" class="pulse-close" aria-label="Закрыть">×</button><img src="/assets/01_calm_right.webp" alt="Помощник" class="pulse-pet"><span class="pulse-kicker">ВАШ ПОМОЩНИК</span><h2 id="pulse-dialog-title">${esc(title)}</h2>${body}</form>`;
    document.body.append(d);d.querySelector('.pulse-close').onclick=()=>d.close();d.addEventListener('close',()=>{d.remove();if(before?.isConnected)before.focus()});
    d.querySelector('form').onsubmit=e=>{e.preventDefault();if(onSubmit?.(new FormData(e.target))!==false)d.close()};d.showModal();
  }
  // A single resumable tour, completed only after every step is acknowledged.
  // Per-section legacy pulseTours flags deliberately do not count as completion.
  let tourActive = false;
  let tourReplay = false;
  const tourSteps = [
    ['home', 'Ваш бизнес на одном экране', 'Здесь собраны универсальные показатели компании: выручка, расходы, маржа, денежный поток, долговая нагрузка и срок оплаты клиентами. Сверху — общая оценка. Выберите период, чтобы сузить подборку материалов.'],
    ['metric/revenue', 'От показателя — к новостям', 'Нажмите на показатель, чтобы увидеть связанные публикации. Откройте первоисточник и обсудите его с помощником. Тематическая связь ещё не доказывает влияние на выручку или прибыль. Свою метрику можно добавить на главной.'],
    ['events', 'Проверяйте основания', 'В новостях собраны доступные материалы. Используйте поиск и фильтры, смотрите дату и характер утверждения: факт, мнение или прогноз. Отраслевая тема относится к материалу; набор показателей бизнеса остаётся универсальным.'],
    ['situations/all', 'Распределяйте работу', 'Ситуации связывают события с особенностями выбранной компании. Возьмите вопрос в работу, оставьте под наблюдением или отложите. Состояние работы помогает отделить новые вопросы от уже проверенных.'],
    ['detail', 'Обсуждайте выводы и оставляйте отзыв', 'В разборе есть основания, альтернативы и история изменений. Проверьте допущение, уточните данные, назначьте следующий шаг. Через меню ситуации можно оставить обратную связь, а через помощника — обсудить вывод.'],
    ['research', 'Сравнивайте периоды', 'Выберите даты и вопрос, который хотите проверить. Анализ периода сопоставляет показатели с материалами за эти даты. В демо значения учебные; отсутствие новостей не означает отсутствие риска.'],
    ['company', 'Расскажите о компании', 'Профиль задаёт контекст оценки: направления деятельности, клиенты, расходы и обязательства. Подтверждайте факты и уточняйте неизвестное. Обсуждение гипотезы само по себе не меняет профиль.'],
    ['sources', 'Управляйте источниками', 'Добавьте материал вручную или исключите неподходящий источник. Прошлые версии оценки сохраняются. Поиск и анализ используют только доступные материалы; загрузка по ссылке пока не подключена.'],
    ['settings', 'Настройте сервис под себя', 'Выберите свой фокус, настройте помощника и предложения банка. Обучение завершено: дальше подсказки не будут открываться автоматически. При необходимости весь тур можно повторить кнопкой вопроса в шапке.'],
  ];
  function tourState() {
    const saved = root().pulseOnboarding;
    if (!saved || saved.version !== 1) root().pulseOnboarding = {version:1, next:0, completedAt:null};
    return root().pulseOnboarding;
  }
  function help(page, force=false) {
    const progress = tourState();
    if (force && !tourActive) { tourReplay=!!progress.completedAt; tourActive=true; progress.next=0; persist(); }
    if (!tourActive && progress.completedAt) return;
    tourActive=true;
    if (document.getElementById('pulse-tour')) return;
    const index=Math.max(0, Math.min(tourSteps.length-1, progress.next || 0));
    const [path,title,text]=tourSteps[index];
    const target=path==='detail' ? 'detail/'+(E.list(state())[0]?.id || 'finance') : path;
    if (location.hash.slice(1)!==target) { navigate(target); return; }
    document.getElementById('pulse-dialog')?.remove();
    const d=document.createElement('dialog'); d.id='pulse-tour'; d.className='pulse-dialog pulse-tour';
    d.setAttribute('aria-labelledby','pulse-tour-title');d.setAttribute('aria-describedby','pulse-tour-description');
    d.innerHTML=`<button type="button" class="tour-collapse" aria-label="Свернуть помощника" aria-expanded="true">−</button><div class="tour-progress"><span>Знакомство с сервисом</span><strong>Шаг ${index+1} из ${tourSteps.length}</strong></div><progress value="${index+1}" max="${tourSteps.length}" aria-label="Прогресс обучения"></progress><img src="/assets/03_listening_right.webp" alt="Ваш помощник" class="pulse-pet"><h2 id="pulse-tour-title">${esc(title)}</h2><p class="tour-copy" aria-hidden="true"></p><p id="pulse-tour-description" class="tour-sr">${esc(text)}</p><p class="tour-status" role="status">Помощник рассказывает…</p><button class="btn primary tour-next" type="button" disabled>${index===tourSteps.length-1?'Понятно, начать работу':'Понятно, дальше'}</button>${tourReplay?'<button type="button" class="tour-exit">Завершить повторный просмотр</button>':''}`;
    let tourFrame = 0;
    document.body.append(d);
    d.addEventListener('cancel',e=>e.preventDefault());
    d.addEventListener('close',()=>{cancelAnimationFrame(tourFrame);d.remove()});
    const collapse=d.querySelector('.tour-collapse');
    collapse.onclick=()=>{const compact=d.classList.toggle('is-collapsed');collapse.textContent=compact?'Продолжить обучение':'−';collapse.setAttribute('aria-label',compact?'Развернуть помощника':'Свернуть помощника');collapse.setAttribute('aria-expanded',String(!compact));};
    const next=d.querySelector('.tour-next'),copy=d.querySelector('.tour-copy');
    let elapsed=0,last=performance.now(),ready=false;
    const duration=matchMedia('(prefers-reduced-motion: reduce)').matches?0:text.length*22;
    function reveal(now) {
      if(!d.isConnected) return;
      if(!document.hidden) elapsed+=Math.min(now-last,100);
      last=now;
      const count=duration?Math.min(text.length,Math.floor(elapsed/22)):text.length;
      copy.textContent=text.slice(0,count);
      if(elapsed>=duration+900){ ready=true;next.disabled=false;d.querySelector('.tour-status').textContent='Прочитайте пояснение и переходите дальше.';return; }
      tourFrame=requestAnimationFrame(reveal);
    }
    next.onclick=()=>{
      if(!ready) return;
      if(index===tourSteps.length-1){progress.completedAt ||= new Date().toISOString();progress.next=0;tourActive=false;tourReplay=false;persist();d.close();navigate('home');}
      else{progress.next=index+1;persist();d.close();d.remove();help(page);}
    };
    d.querySelector('.tour-exit')?.addEventListener('click',()=>{tourActive=false;tourReplay=false;persist();d.close()});
    d.show();tourFrame=requestAnimationFrame(reveal);
  }
  function action(type,id,page) {
    if(type==='pulse-help'){help(page,true);return true;}
    if(type==='pulse-period'){
      if(id==='custom')dialog('Выберите период',`<label>С <input required type="date" name="start" value="${customRange.start}"></label><label>По <input required type="date" name="end" value="${customRange.end}"></label><p class="pulse-error" role="alert"></p><button class="btn primary">Применить</button>`,f=>{if(f.get('start')>f.get('end')){document.querySelector('.pulse-error').textContent='Дата окончания должна быть не раньше начала.';return false}customRange={start:f.get('start'),end:f.get('end')};period=id;render()});
      else{period=id;render()}return true;
    }
    if(type==='pulse-add'){
      dialog('Что важно вашему бизнесу?',`<p>Опишите показатель и выберите тему. В демо подборка строится по теме; персональный поиск ИИ будет подключён на следующем этапе.</p><label>Название<input required name="name" maxlength="80" placeholder="Например, стоимость доставки"></label><label>Что измеряем<textarea required name="description" maxlength="1000" placeholder="Средние затраты на доставку одной партии"></textarea></label><label>Единица измерения<input required name="unit" maxlength="20" placeholder="₽ / партия"></label><label>Связанная тема<select name="domain">${C.domains.filter(d=>!['growth','context'].includes(d.id)).map(d=>`<option value="${d.id}">${esc(d.label)}</option>`).join('')}</select></label><button class="btn primary">Добавить показатель</button>`,f=>{state().pulseMetrics||=[];state().pulseMetrics.push({id:crypto.randomUUID(),name:f.get('name').trim(),description:f.get('description').trim(),unit:f.get('unit').trim(),domain:f.get('domain')});persist();render()});return true;
    }
    return false;
  }
  return {sidebar,dashboard,metricPage,coreDashboard,coreMetricPage,action,help};
}
