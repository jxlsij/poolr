(() => {
  const app = document.getElementById("app");
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const onboardingKey = "poolr:onboarding:v1";
  const walletKey = "poolr:wallet:v1";
  const params = new URLSearchParams(window.location.search);
  const hasTelegramAuth = Boolean(tg && tg.initData);
  const targetMarketId = params.get("market_id") || params.get("market");
  let noticeTimer = null;

  const demoMarkets = [
    {
      id: 1001,
      question: "Will Poolr reach 100 markets?",
      options: ["Yes", "No"],
      status: "active",
      total_pool: 242,
      min_bet: 5,
      bets_count: 31,
      pool_by_option: { 0: 176, 1: 66 },
      odds: { 0: 0.7273, 1: 0.2727 },
      deadline: new Date(Date.now() + 1000 * 60 * 60 * 7).toISOString(),
      created_at: new Date(Date.now() - 1000 * 60 * 31).toISOString(),
    },
    {
      id: 1002,
      question: "Will tonight's match go to extra time?",
      options: ["Yes", "No"],
      status: "active",
      total_pool: 97,
      min_bet: 3,
      bets_count: 14,
      pool_by_option: { 0: 42, 1: 55 },
      odds: { 0: 0.433, 1: 0.567 },
      deadline: new Date(Date.now() + 1000 * 60 * 60 * 2).toISOString(),
      created_at: new Date(Date.now() - 1000 * 60 * 76).toISOString(),
    },
    {
      id: 1003,
      question: "Will the next feature ship before Friday?",
      options: ["Ships", "Slips"],
      status: "resolved",
      winning_option: 0,
      total_pool: 338,
      min_bet: 10,
      bets_count: 46,
      pool_by_option: { 0: 214, 1: 124 },
      odds: { 0: 0.6331, 1: 0.3669 },
      deadline: new Date(Date.now() - 1000 * 60 * 60 * 6).toISOString(),
      resolved_at: new Date(Date.now() - 1000 * 60 * 42).toISOString(),
      created_at: new Date(Date.now() - 1000 * 60 * 60 * 19).toISOString(),
    },
  ];

  const demoProfile = {
    user_id: 0,
    username: "preview",
    first_name: "Poolr",
    balance: 0,
    stats: {
      bets_count: 12,
      markets_created: 4,
      total_staked: 420,
      total_won: 628,
      pending_withdrawals: 1,
    },
  };

  const demoBets = [
    {
      id: 501,
      market_id: 1001,
      option_index: 0,
      stars_amount: 25,
      created_at: new Date(Date.now() - 1000 * 60 * 22).toISOString(),
      market: demoMarkets[0],
    },
    {
      id: 502,
      market_id: 1003,
      option_index: 0,
      stars_amount: 50,
      created_at: new Date(Date.now() - 1000 * 60 * 60 * 12).toISOString(),
      market: demoMarkets[2],
    },
  ];

  const state = {
    ready: false,
    loading: true,
    usingDemo: !hasTelegramAuth,
    view: "markets",
    sort: "stars",
    filter: "active",
    profile: demoProfile,
    markets: demoMarkets,
    bets: demoBets,
    withdrawals: [],
    selectedMarket: null,
    selectedOption: 0,
    stakeAmount: 25,
    walletAddress: safeStorageGet(walletKey) || "",
    depositAmount: 25,
    withdrawAmount: 25,
    busy: "",
    notice: "",
    onboardingIndex: 0,
    showOnboarding: shouldShowOnboarding(),
  };

  initTelegram();
  app.addEventListener("click", handleClick);
  app.addEventListener("input", handleInput);
  render();
  loadData();

  function initTelegram() {
    if (!tg) {
      return;
    }
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor("#000000");
      tg.setBackgroundColor("#eaf7ff");
      tg.enableClosingConfirmation();
    } catch (error) {
      console.warn("Telegram WebApp setup skipped", error);
    }
  }

  function shouldShowOnboarding() {
    if (targetMarketId) {
      return false;
    }
    if (params.get("onboarding") === "1") {
      return true;
    }
    return safeStorageGet(onboardingKey) !== "done";
  }

  async function loadData() {
    if (!hasTelegramAuth) {
      state.loading = false;
      state.ready = true;
      state.usingDemo = true;
      await openTargetMarketFromParams();
      render();
      return;
    }

    try {
      const [profile, marketFeed, betsFeed, withdrawalsFeed] = await Promise.all([
        apiGet("/api/profile"),
        apiGet("/api/markets?status=all&limit=50"),
        apiGet("/api/bets?limit=50"),
        apiGet("/api/withdrawals?limit=50"),
      ]);
      state.profile = profile;
      state.markets = Array.isArray(marketFeed.markets) ? marketFeed.markets : [];
      state.bets = Array.isArray(betsFeed.bets) ? betsFeed.bets : [];
      state.withdrawals = Array.isArray(withdrawalsFeed.withdrawals) ? withdrawalsFeed.withdrawals : [];
      await openTargetMarketFromParams();
      state.usingDemo = false;
    } catch (error) {
      console.warn("Mini App API fallback enabled", error);
      state.usingDemo = true;
      showNotice("Preview mode is on. Open inside Telegram to send invoices.");
    } finally {
      state.loading = false;
      state.ready = true;
      render();
    }
  }

  async function openTargetMarketFromParams() {
    if (!targetMarketId) {
      return;
    }

    let market = state.markets.find((candidate) => String(candidate.id) === String(targetMarketId));
    if (!market && hasTelegramAuth) {
      try {
        market = await apiGet(`/api/market/${encodeURIComponent(targetMarketId)}`);
        state.markets = [market, ...state.markets.filter((candidate) => String(candidate.id) !== String(market.id))];
      } catch (error) {
        console.warn("Could not open linked market", error);
        showNotice("Could not open this market.");
        return;
      }
    }
    if (!market) {
      return;
    }

    state.view = "markets";
    state.filter = "all";
    state.selectedMarket = market;
    state.selectedOption = 0;
    state.stakeAmount = Math.max(Number(market.min_bet || 1), state.stakeAmount || 1);
  }

  async function apiGet(path) {
    const response = await fetch(path, {
      headers: authHeaders(),
    });
    return readApiResponse(response);
  }

  async function apiPost(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        ...authHeaders(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return readApiResponse(response);
  }

  function authHeaders() {
    if (!hasTelegramAuth) {
      return {};
    }
    return {
      Authorization: `tma ${tg.initData}`,
    };
  }

  async function readApiResponse(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = data && data.error && data.error.message ? data.error.message : "Request failed.";
      throw new Error(message);
    }
    return data;
  }

  function render() {
    if (state.showOnboarding) {
      renderOnboarding();
      return;
    }

    const viewHtml =
      state.view === "wallet"
        ? walletView()
        : state.view === "activity"
          ? activityView()
          : marketsView();

    app.innerHTML = `
      ${viewHtml}
      ${bottomNav()}
      ${state.selectedMarket ? betSheet(state.selectedMarket) : ""}
      ${state.notice ? `<div class="toast">${escapeHtml(state.notice)}</div>` : ""}
    `;
  }

  function renderOnboarding() {
    const slides = onboardingSlides();
    const slide = slides[state.onboardingIndex] || slides[0];
    const isLast = state.onboardingIndex === slides.length - 1;

    app.innerHTML = `
      <main class="onboarding">
        <section class="onboarding-card slide-${state.onboardingIndex}" aria-label="Poolr onboarding">
          <div class="onboarding-art">
            <img class="onboarding-asset" src="${slide.art}" alt="" aria-hidden="true" />
          </div>
          <h1 class="onboarding-title">${escapeHtml(slide.title)}</h1>
          ${
            slide.disclaimers.length
              ? `<ul class="disclaimer-list">${slide.disclaimers
                  .map((item) => `<li>${escapeHtml(item)}</li>`)
                  .join("")}</ul>`
              : ""
          }
          <div class="onboarding-bottom">
            <div class="dots" aria-label="Onboarding progress">
              ${slides
                .map(
                  (_, index) =>
                    `<span class="dot ${index === state.onboardingIndex ? "active" : ""}"></span>`,
                )
                .join("")}
            </div>
            <button class="primary-btn onboarding-btn" type="button" data-next-onboarding>
              ${isLast ? "LFG" : "Next"}
            </button>
          </div>
        </section>
      </main>
    `;
  }

  function marketsView() {
    const markets = filteredMarkets();
    const activeCount = state.markets.filter((market) => market.status === "active").length;
    const resolvedCount = state.markets.filter((market) => market.status === "resolved").length;
    const volume = state.markets.reduce((sum, market) => sum + marketTotalPool(market), 0);

    return `
      <main class="view">
        <section class="hero-card">
          <div class="brand-row">
            <div class="brand-lockup">
              ${brandMark()}
              <div class="brand-name">Poolr</div>
            </div>
            <div class="round-badge">${formatNumber(state.profile.balance || 0)}</div>
          </div>
          <div class="hero-copy">
            <p>Group prediction markets</p>
            <strong>${formatNumber(volume)} Stars</strong>
            <span>Staked across live and resolved markets</span>
          </div>
          <div class="hero-stats">
            <div class="stat-glass">
              <strong>${activeCount}</strong>
              <span>Active</span>
            </div>
            <div class="stat-glass">
              <strong>${resolvedCount}</strong>
              <span>Resolved</span>
            </div>
          </div>
        </section>

        <div class="toolbar-row">
          <div class="section-heading">
            <h2>Markets</h2>
            <p>${state.loading ? "Syncing" : `${state.markets.length} launches`}</p>
          </div>
          <div class="pill-toggle" role="tablist" aria-label="Sort markets">
            <button type="button" class="${state.sort === "stars" ? "active" : ""}" data-sort="stars">By Stars</button>
            <button type="button" class="${state.sort === "new" ? "active" : ""}" data-sort="new">New</button>
          </div>
        </div>

        <div class="filter-tabs" role="tablist" aria-label="Filter markets">
          ${["active", "resolved", "all"]
            .map(
              (filter) =>
                `<button type="button" class="${state.filter === filter ? "active" : ""}" data-filter="${filter}">${capitalize(
                  filter,
                )}</button>`,
            )
            .join("")}
        </div>

        <section class="market-list">
          ${
            markets.length
              ? markets.map((market, index) => marketCard(market, index)).join("")
              : emptyCard("No markets yet", "Create one from a Telegram group with /bet or @pooolr_bot.")
          }
        </section>
      </main>
    `;
  }

  function walletView() {
    const address = state.walletAddress.trim();
    const hasAddress = Boolean(address);
    const short = hasAddress ? shortenAddress(address) : "Connect wallet";
    const pendingWithdrawals = state.withdrawals.filter((withdrawal) => withdrawal.status === "pending").length;

    return `
      <main class="view">
        <div class="view-title-row">
          <div class="title-block">
            <h1>Wallet</h1>
            <p>Connect once, then receive beta payouts.</p>
          </div>
        </div>

        <section class="wallet-panel">
          <div class="wallet-head">
            <div class="wallet-icon-box">${walletIcon()}</div>
            <div>
              <h2>${hasAddress ? "Wallet connected" : "Wallet ready"}</h2>
              <p>${hasAddress ? "TON wallet" : "Manual TON-equivalent payouts"}</p>
            </div>
          </div>

          <div class="address-card">
            <span class="label">Address</span>
            <strong class="address-large">${escapeHtml(short)}</strong>
            <span class="address-small">${escapeHtml(hasAddress ? address : "Paste a TON wallet address before requesting payout.")}</span>
          </div>

          <div class="wallet-actions">
            <button class="secondary-btn" type="button" data-action="copy-wallet" ${hasAddress ? "" : "disabled"}>Copy address</button>
            <button class="primary-btn" type="button" data-action="open-wallet">Open @wallet</button>
          </div>

          <button class="wallet-select" type="button" data-action="save-wallet">${escapeHtml(short)}</button>

          <div class="balance-row">
            <div class="mini-stat">
              <span>Withdrawable</span>
              <strong>${formatNumber(state.profile.balance || 0)}</strong>
            </div>
            <div class="mini-stat">
              <span>Pending</span>
              <strong>${pendingWithdrawals}</strong>
            </div>
          </div>

          <div class="form-grid">
            <label class="field">
              <span>TON wallet</span>
              <input
                type="text"
                autocomplete="off"
                inputmode="text"
                value="${escapeAttr(state.walletAddress)}"
                placeholder="EQ..."
                data-input="wallet"
              />
            </label>
            <label class="field">
              <span>Withdraw Stars</span>
              <input type="number" min="1" step="1" value="${escapeAttr(String(state.withdrawAmount))}" data-input="withdraw" />
            </label>
            <button class="primary-btn" type="button" data-action="withdraw" ${state.busy === "withdraw" ? "disabled" : ""}>
              ${state.busy === "withdraw" ? "Creating request" : "Request payout"}
            </button>
            <label class="field">
              <span>Add Stars</span>
              <input type="number" min="1" step="1" value="${escapeAttr(String(state.depositAmount))}" data-input="deposit" />
            </label>
            <button class="secondary-btn" type="button" data-action="deposit" ${state.busy === "deposit" ? "disabled" : ""}>
              ${state.busy === "deposit" ? "Sending invoice" : "Send invoice"}
            </button>
          </div>
        </section>
      </main>
    `;
  }

  function activityView() {
    const items = [
      ...state.bets.map((bet) => ({ kind: "bet", item: bet, created_at: bet.created_at })),
      ...state.withdrawals.map((withdrawal) => ({ kind: "withdrawal", item: withdrawal, created_at: withdrawal.created_at })),
    ].sort((left, right) => new Date(right.created_at || 0) - new Date(left.created_at || 0));

    return `
      <main class="view">
        <div class="view-title-row">
          <div class="title-block">
            <h1>Activity</h1>
            <p>Your stakes, wins, and payout requests.</p>
          </div>
          <button class="ghost-btn" type="button" data-action="refresh">Refresh</button>
        </div>

        <div class="balance-row">
          <div class="mini-stat">
            <span>Total staked</span>
            <strong>${formatNumber(state.profile.stats ? state.profile.stats.total_staked : 0)}</strong>
          </div>
          <div class="mini-stat">
            <span>Total won</span>
            <strong>${formatNumber(state.profile.stats ? state.profile.stats.total_won : 0)}</strong>
          </div>
        </div>

        <section class="activity-list">
          ${items.length ? items.map(activityCard).join("") : emptyCard("No activity yet", "Your market stakes and payout requests will land here.")}
        </section>
      </main>
    `;
  }

  function bottomNav() {
    const items = [
      { view: "markets", icon: coinStackIcon(), label: "Markets" },
      { view: "activity", icon: tonIcon(), label: "Activity" },
      { view: "wallet", icon: walletIcon(), label: "Wallet" },
    ];

    return `
      <nav class="bottom-nav" aria-label="Poolr navigation">
        ${items
          .map(
            (item) => `
              <button
                type="button"
                class="nav-item ${state.view === item.view ? "active" : ""}"
                data-view="${item.view}"
                aria-label="${item.label}"
                title="${item.label}"
              >
                ${item.icon}
              </button>
            `,
          )
          .join("")}
      </nav>
    `;
  }

  function marketCard(market, index) {
    const total = marketTotalPool(market);
    const goal = Math.max(100, Math.ceil(total / 100) * 100 || 100);
    const progress = Math.max(4, Math.min(100, Math.round((total / goal) * 100)));
    const options = normalizeOptions(market);
    const odds = normalizeOdds(market, options);
    const logoClass = ["", "purple", "blue", "green"][index % 4];
    const status = market.status || "active";
    const statusClass = status === "active" ? "" : status;
    const logo = marketLogoText(market);

    return `
      <button class="market-card" type="button" data-market-id="${escapeAttr(String(market.id))}">
        <span class="market-logo ${logoClass}">${escapeHtml(logo)}</span>
        <span class="market-main">
          <span class="market-topline">
            <span class="market-question">${escapeHtml(market.question || "Untitled market")}</span>
            <span class="status-pill ${statusClass}">${statusLabel(status)}</span>
          </span>
          <span class="market-meta">
            <span>${market.bets_count || 0} bets</span>
            <span class="market-pool">${formatNumber(total)} / ${formatNumber(goal)} Stars</span>
          </span>
          <span class="progress-track" style="--progress: ${progress}%">
            <span class="progress-fill"></span>
          </span>
          <span class="option-strip">
            ${options
              .slice(0, 2)
              .map(
                (option, optionIndex) => `
                  <span class="option-chip">
                    ${escapeHtml(option)} <strong>${formatPercent(odds[optionIndex] || 0)}</strong>
                  </span>
                `,
              )
              .join("")}
          </span>
        </span>
      </button>
    `;
  }

  function betSheet(market) {
    const options = normalizeOptions(market);
    const odds = normalizeOdds(market, options);
    const selectedOption = options[state.selectedOption] || options[0];
    const total = marketTotalPool(market);
    const estimated = estimatePayout(state.stakeAmount, state.selectedOption, market);

    return `
      <div class="sheet-backdrop" data-close-sheet>
        <section class="bet-sheet" role="dialog" aria-modal="true" aria-label="Place bet" data-sheet>
          <div class="sheet-top">
            <h2>${escapeHtml(market.question || "Untitled market")}</h2>
            <button class="sheet-close" type="button" data-close-sheet aria-label="Close">x</button>
          </div>

          <div class="odds-grid">
            ${options
              .map(
                (option, index) => `
                  <button class="odds-btn ${state.selectedOption === index ? "active" : ""}" type="button" data-option-index="${index}">
                    <span>${formatPercent(odds[index] || 0)} pool</span>
                    <strong>${escapeHtml(option)}</strong>
                  </button>
                `,
              )
              .join("")}
          </div>

          <label class="field">
            <span>Stake amount</span>
            <input type="number" min="${Number(market.min_bet || 1)}" step="1" value="${escapeAttr(
              String(state.stakeAmount),
            )}" data-input="stake" />
          </label>

          <div class="stake-grid">
            ${[5, 10, 25, 50]
              .map(
                (amount) =>
                  `<button class="stake-btn ${state.stakeAmount === amount ? "active" : ""}" type="button" data-stake="${amount}">${amount}</button>`,
              )
              .join("")}
          </div>

          <div class="sheet-summary">
            <div class="summary-line">
              <span>Selected</span>
              <strong>${escapeHtml(selectedOption)}</strong>
            </div>
            <div class="summary-line">
              <span>Current pool</span>
              <strong>${formatNumber(total)} Stars</strong>
            </div>
            <div class="summary-line">
              <span>Estimated return</span>
              <strong>${formatNumber(estimated)} Stars</strong>
            </div>
            <div class="summary-line">
              <span>Minimum</span>
              <strong>${formatNumber(market.min_bet || 1)} Stars</strong>
            </div>
          </div>

          <div class="sheet-actions">
            <button class="primary-btn" type="button" data-action="place-bet" ${state.busy === "bet" ? "disabled" : ""}>
              ${state.busy === "bet" ? "Sending invoice" : "Pay with Stars"}
            </button>
            <button class="secondary-btn" type="button" data-close-sheet>Cancel</button>
          </div>
        </section>
      </div>
    `;
  }

  function activityCard(entry) {
    if (entry.kind === "withdrawal") {
      const withdrawal = entry.item;
      return `
        <article class="activity-card">
          <h3>Payout request</h3>
          <p>${formatNumber(withdrawal.stars_amount || 0)} Stars to ${escapeHtml(shortenAddress(withdrawal.ton_wallet_address || ""))}</p>
          <div class="activity-meta">
            <span class="meta-pill">${escapeHtml(statusLabel(withdrawal.status || "pending"))}</span>
            <span class="meta-pill">${escapeHtml(formatDate(withdrawal.created_at))}</span>
          </div>
        </article>
      `;
    }

    const bet = entry.item;
    const market = bet.market || state.markets.find((candidate) => String(candidate.id) === String(bet.market_id)) || {};
    const options = normalizeOptions(market);
    return `
      <article class="activity-card">
        <h3>${escapeHtml(market.question || "Market stake")}</h3>
        <p>${formatNumber(bet.stars_amount || 0)} Stars on ${escapeHtml(options[bet.option_index] || `Option ${bet.option_index + 1}`)}</p>
        <div class="activity-meta">
          <span class="meta-pill">${escapeHtml(statusLabel(market.status || "active"))}</span>
          <span class="meta-pill">${escapeHtml(formatDate(bet.created_at))}</span>
        </div>
      </article>
    `;
  }

  function emptyCard(title, body) {
    return `
      <div class="empty-card">
        <div>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(body)}</span>
        </div>
      </div>
    `;
  }

  function onboardingSlides() {
    return [
      {
        title: "Create a market from any chat for free",
        art: "/app/static/assets/onboarding-chat.png",
        disclaimers: [],
      },
      {
        title: "Stake Stars before anyone else",
        art: "/app/static/assets/onboarding-stake.png",
        disclaimers: [],
      },
      {
        title: "Beta payouts are reviewed",
        art: "/app/static/assets/onboarding-payout.png",
        disclaimers: [
          "Prediction markets can be risky. Stake only what you can afford.",
          "Winnings accrue in Stars units before TON-equivalent payout review.",
          "Use Poolr only where local rules allow this kind of market.",
        ],
      },
    ];
  }

  function handleClick(event) {
    const target = event.target;
    const nextButton = target.closest("[data-next-onboarding]");
    if (nextButton) {
      nextOnboarding();
      return;
    }

    const viewButton = target.closest("[data-view]");
    if (viewButton) {
      state.view = viewButton.dataset.view;
      state.selectedMarket = null;
      haptic("light");
      render();
      return;
    }

    const sortButton = target.closest("[data-sort]");
    if (sortButton) {
      state.sort = sortButton.dataset.sort;
      haptic("light");
      render();
      return;
    }

    const filterButton = target.closest("[data-filter]");
    if (filterButton) {
      state.filter = filterButton.dataset.filter;
      haptic("light");
      render();
      return;
    }

    const marketButton = target.closest("[data-market-id]");
    if (marketButton) {
      const market = state.markets.find((candidate) => String(candidate.id) === marketButton.dataset.marketId);
      if (market) {
        state.selectedMarket = market;
        state.selectedOption = 0;
        state.stakeAmount = Math.max(Number(market.min_bet || 1), state.stakeAmount || 1);
        haptic("medium");
        render();
      }
      return;
    }

    const optionButton = target.closest("[data-option-index]");
    if (optionButton) {
      state.selectedOption = Number(optionButton.dataset.optionIndex || 0);
      haptic("light");
      render();
      return;
    }

    const stakeButton = target.closest("[data-stake]");
    if (stakeButton) {
      state.stakeAmount = Number(stakeButton.dataset.stake || 1);
      haptic("light");
      render();
      return;
    }

    const closeSheet = target.closest("[data-close-sheet]");
    if (closeSheet && !target.closest("[data-sheet]")) {
      state.selectedMarket = null;
      render();
      return;
    }
    if (closeSheet && target.closest(".sheet-close, .secondary-btn")) {
      state.selectedMarket = null;
      render();
      return;
    }

    const action = target.closest("[data-action]");
    if (action) {
      runAction(action.dataset.action);
    }
  }

  function handleInput(event) {
    const target = event.target;
    if (!target || !target.dataset) {
      return;
    }
    const input = target.dataset.input;
    if (input === "stake") {
      state.stakeAmount = Math.max(1, Number(target.value || 1));
      render();
    }
    if (input === "wallet") {
      state.walletAddress = target.value;
      safeStorageSet(walletKey, state.walletAddress.trim());
    }
    if (input === "withdraw") {
      state.withdrawAmount = Math.max(1, Number(target.value || 1));
    }
    if (input === "deposit") {
      state.depositAmount = Math.max(1, Number(target.value || 1));
    }
  }

  function nextOnboarding() {
    const slides = onboardingSlides();
    if (state.onboardingIndex < slides.length - 1) {
      state.onboardingIndex += 1;
      haptic("light");
      render();
      return;
    }
    safeStorageSet(onboardingKey, "done");
    state.showOnboarding = false;
    haptic("medium");
    render();
  }

  async function runAction(action) {
    if (action === "refresh") {
      state.loading = true;
      render();
      await loadData();
      showNotice("Poolr is up to date.");
      return;
    }
    if (action === "copy-wallet") {
      await copyWallet();
      return;
    }
    if (action === "open-wallet") {
      openWallet();
      return;
    }
    if (action === "save-wallet") {
      safeStorageSet(walletKey, state.walletAddress.trim());
      showNotice(state.walletAddress.trim() ? "Wallet saved." : "Paste a TON wallet address first.");
      return;
    }
    if (action === "place-bet") {
      await placeBet();
      return;
    }
    if (action === "deposit") {
      await requestDeposit();
      return;
    }
    if (action === "withdraw") {
      await requestWithdrawal();
    }
  }

  async function placeBet() {
    const market = state.selectedMarket;
    if (!market) {
      return;
    }
    if (market.status && market.status !== "active") {
      showNotice("This market is not accepting stakes.");
      return;
    }
    if (!hasTelegramAuth) {
      state.selectedMarket = null;
      showNotice("Preview mode: Telegram will send a Stars invoice here.");
      return;
    }

    state.busy = "bet";
    render();
    try {
      const marketId = market.id;
      await apiPost("/api/bet", {
        market_id: marketId,
        option_index: state.selectedOption,
        stars_amount: state.stakeAmount,
      });
      state.selectedMarket = null;
      state.busy = "";
      showNotice("Stars invoice sent. Updating after payment.");
      void refreshAfterInvoice(marketId);
    } catch (error) {
      showNotice(error.message || "Could not send Stars invoice.");
    } finally {
      state.busy = "";
      render();
    }
  }

  async function refreshAfterInvoice(marketId) {
    for (const waitMs of [1500, 3000, 6000]) {
      await delay(waitMs);
      await loadData();
      const hasRecordedBet = state.bets.some((bet) => String(bet.market_id) === String(marketId));
      if (hasRecordedBet) {
        showNotice("Bet recorded.");
        return;
      }
    }
    showNotice("Invoice sent. Tap refresh if the pool still looks old.");
  }

  async function requestDeposit() {
    if (!hasTelegramAuth) {
      showNotice("Preview mode: a Stars top-up invoice appears in Telegram.");
      return;
    }
    state.busy = "deposit";
    render();
    try {
      await apiPost("/api/deposit", {
        stars_amount: state.depositAmount,
      });
      showNotice("Stars invoice sent.");
    } catch (error) {
      showNotice(error.message || "Could not send deposit invoice.");
    } finally {
      state.busy = "";
      render();
    }
  }

  async function requestWithdrawal() {
    const wallet = state.walletAddress.trim();
    if (!wallet) {
      showNotice("Paste a TON wallet address first.");
      return;
    }
    safeStorageSet(walletKey, wallet);
    if (!hasTelegramAuth) {
      showNotice("Preview mode: payout request would be sent for review.");
      return;
    }

    state.busy = "withdraw";
    render();
    try {
      await apiPost("/api/withdraw", {
        stars_amount: state.withdrawAmount,
        ton_wallet_address: wallet,
      });
      showNotice("Payout request created.");
      await loadData();
    } catch (error) {
      showNotice(error.message || "Could not create payout request.");
    } finally {
      state.busy = "";
      render();
    }
  }

  async function copyWallet() {
    const wallet = state.walletAddress.trim();
    if (!wallet) {
      showNotice("Paste a TON wallet address first.");
      return;
    }
    try {
      await navigator.clipboard.writeText(wallet);
      showNotice("Wallet address copied.");
      haptic("light");
    } catch (error) {
      showNotice(wallet);
    }
  }

  function openWallet() {
    const url = "https://t.me/wallet";
    if (tg && typeof tg.openTelegramLink === "function") {
      tg.openTelegramLink(url);
    } else {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  }

  function filteredMarkets() {
    const filtered = state.markets.filter((market) => {
      if (state.filter === "all") {
        return true;
      }
      return market.status === state.filter;
    });
    return [...filtered].sort((left, right) => {
      if (state.sort === "new") {
        return new Date(right.created_at || 0) - new Date(left.created_at || 0);
      }
      return marketTotalPool(right) - marketTotalPool(left);
    });
  }

  function normalizeOptions(market) {
    const options = Array.isArray(market.options) && market.options.length ? market.options : ["Yes", "No"];
    return options.map((option) => String(option));
  }

  function normalizeOdds(market, options) {
    if (market.odds && typeof market.odds === "object") {
      return options.map((_, index) => Number(market.odds[index] || market.odds[String(index)] || 0));
    }
    const pools = options.map((_, index) => Number((market.pool_by_option || {})[index] || (market.pool_by_option || {})[String(index)] || 0));
    const total = pools.reduce((sum, value) => sum + value, 0);
    return pools.map((value) => (total > 0 ? value / total : 0));
  }

  function marketTotalPool(market) {
    if (Number.isFinite(Number(market.total_pool))) {
      return Number(market.total_pool);
    }
    return Object.values(market.pool_by_option || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  }

  function estimatePayout(stake, optionIndex, market) {
    const options = normalizeOptions(market);
    const pools = options.map((_, index) => Number((market.pool_by_option || {})[index] || (market.pool_by_option || {})[String(index)] || 0));
    const totalPool = pools.reduce((sum, value) => sum + value, 0) + stake;
    const selectedPool = (pools[optionIndex] || 0) + stake;
    if (selectedPool <= 0) {
      return stake;
    }
    return Math.max(1, Math.floor((stake / selectedPool) * totalPool * 0.92));
  }

  function marketLogoText(market) {
    const text = String(market.question || "PO");
    const words = text.match(/[A-Za-z0-9]+/g) || ["PO"];
    const first = words[0] || "PO";
    return first.length <= 3 ? first.toUpperCase() : first.slice(0, 3).toUpperCase();
  }

  function statusLabel(status) {
    const labels = {
      active: "Open",
      closed: "Closed",
      resolved: "Resolved",
      cancelled: "Refunded",
      disputed: "Disputed",
      pending: "Pending",
      completed: "Paid",
      failed: "Rejected",
    };
    return labels[status] || capitalize(status || "Open");
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value || 0));
  }

  function formatPercent(value) {
    return `${Math.round(Number(value || 0) * 100)}%`;
  }

  function formatDate(value) {
    if (!value) {
      return "Now";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "Now";
    }
    return new Intl.DateTimeFormat("en", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function shortenAddress(value) {
    if (!value) {
      return "Connect wallet";
    }
    if (value.length <= 14) {
      return value;
    }
    return `${value.slice(0, 6)}...${value.slice(-6)}`;
  }

  function capitalize(value) {
    const text = String(value || "");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function showNotice(message) {
    state.notice = message;
    if (noticeTimer) {
      window.clearTimeout(noticeTimer);
    }
    noticeTimer = window.setTimeout(() => {
      state.notice = "";
      render();
    }, 3600);
    render();
  }

  function delay(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function haptic(style) {
    try {
      if (tg && tg.HapticFeedback && typeof tg.HapticFeedback.impactOccurred === "function") {
        tg.HapticFeedback.impactOccurred(style);
      }
    } catch (error) {
      console.warn("Haptic feedback skipped", error);
    }
  }

  function safeStorageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return "";
    }
  }

  function safeStorageSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      console.warn("localStorage write skipped", error);
    }
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }

  function brandMark() {
    return `
      <svg class="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
        <path d="M32 7 58 52H6L32 7Z" fill="none" stroke="rgba(255,255,255,.92)" stroke-width="5" stroke-linejoin="round" />
        <path d="M16 34c6-9 26-9 32 0-6 9-26 9-32 0Z" fill="rgba(255,255,255,.35)" stroke="rgba(255,255,255,.92)" stroke-width="4" />
        <circle cx="32" cy="34" r="8" fill="#ffffff" />
        <circle cx="32" cy="34" r="4" fill="#7fc4f7" />
      </svg>
    `;
  }

  function coinStackIcon() {
    return `
      <svg class="nav-icon" viewBox="0 0 48 48" aria-hidden="true">
        <g fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
          <ellipse cx="18" cy="30" rx="12" ry="5" fill="rgba(116,190,233,.18)" />
          <path d="M6 30v7c0 3 5 5 12 5s12-2 12-5v-7" />
          <ellipse cx="30" cy="17" rx="12" ry="5" fill="rgba(116,190,233,.18)" />
          <path d="M18 17v16c0 3 5 5 12 5s12-2 12-5V17" />
          <path d="M18 25c0 3 5 5 12 5s12-2 12-5" />
        </g>
      </svg>
    `;
  }

  function tonIcon() {
    return `
      <svg class="nav-icon" viewBox="0 0 48 48" aria-hidden="true">
        <path d="M7 10h34L24 40 7 10Z" fill="rgba(116,190,233,.18)" stroke="currentColor" stroke-width="4" stroke-linejoin="round" />
        <path d="M24 40V10M7 10l17 30 17-30" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    `;
  }

  function walletIcon() {
    return `
      <svg class="nav-icon" viewBox="0 0 48 48" aria-hidden="true">
        <path d="M9 13h26c4 0 7 3 7 7v15c0 4-3 7-7 7H9c-4 0-7-3-7-7V20c0-4 3-7 7-7Z" fill="rgba(116,190,233,.18)" stroke="currentColor" stroke-width="4" stroke-linejoin="round" />
        <path d="M33 24h10v10H33c-3 0-5-2-5-5s2-5 5-5Z" fill="#fff" stroke="currentColor" stroke-width="4" stroke-linejoin="round" />
        <circle cx="34" cy="29" r="2.4" fill="currentColor" />
        <path d="M12 13V9c0-2 2-4 4-4h16" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" />
      </svg>
    `;
  }

  function chatArt() {
    return `
      <svg class="mascot-svg" viewBox="0 0 420 330" aria-hidden="true">
        <defs>
          <linearGradient id="chatBody" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stop-color="#f2f8ff" />
            <stop offset="1" stop-color="#d8e6f0" />
          </linearGradient>
          <linearGradient id="yellowGloss" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stop-color="#fff05a" />
            <stop offset="1" stop-color="#ffc51d" />
          </linearGradient>
        </defs>
        <g class="float-soft">
          <rect x="112" y="22" width="196" height="165" rx="18" fill="url(#chatBody)" stroke="#aab8c6" stroke-width="8" />
          <rect x="112" y="22" width="196" height="46" rx="18" fill="#f5f5f5" stroke="#c9c9c9" stroke-width="8" />
          <path d="M143 45h28" stroke="#2b82dd" stroke-width="7" stroke-linecap="round" />
          <path d="M144 45l14-14M144 45l14 14" stroke="#2b82dd" stroke-width="7" stroke-linecap="round" />
          <circle cx="282" cy="45" r="18" fill="#32a8ec" />
          <path d="m274 45 14-8-4 17-5-5-5 3 2-5Z" fill="#fff" />
          <path d="M169 95h91c19 0 35 16 35 35v18c0 19-16 35-35 35h-99c-15 0-28-9-33-22-5 10-15 16-28 17 12-8 17-17 17-33v-15c0-19 16-35 35-35h17Z" fill="#fff" />
          <text x="151" y="126" fill="#102b4c" font-size="23" font-weight="900" font-family="Arial, sans-serif">Yo @pooolr_bot</text>
          <text x="151" y="154" fill="#102b4c" font-size="23" font-weight="900" font-family="Arial, sans-serif">Will it happen?</text>
          <circle cx="209" cy="171" r="8" fill="#ffd329" />
        </g>
        <g class="bounce-soft">
          <path d="M87 205c-29 8-45 35-34 66 12 33 49 39 87 35 33-4 43-25 31-51-7-16-12-31-8-48 5-24-35-13-76-2Z" fill="url(#yellowGloss)" stroke="#ff9718" stroke-width="8" />
          <circle cx="142" cy="235" r="15" fill="#050505" />
          <circle cx="148" cy="229" r="5" fill="#fff" />
          <path d="M159 247c14 5 23 13 21 23-7 2-18 0-26-6" fill="#f65a1f" stroke="#d94712" stroke-width="6" stroke-linecap="round" />
          <path d="M76 222c-10 15-15 30-13 47M121 264c4 16 11 27 21 35M139 260c3 16 9 28 17 36" fill="none" stroke="#ff9718" stroke-width="8" stroke-linecap="round" />
          <path d="M78 221c5-19 20-34 44-38" fill="none" stroke="#fff48e" stroke-width="8" stroke-linecap="round" />
        </g>
        <g class="bounce-soft float-soft-delayed">
          <path d="M275 205c29 8 45 35 34 66-12 33-49 39-87 35-33-4-43-25-31-51 7-16 12-31 8-48-5-24 35-13 76-2Z" fill="url(#yellowGloss)" stroke="#ff9718" stroke-width="8" />
          <circle cx="220" cy="235" r="15" fill="#050505" />
          <circle cx="226" cy="229" r="5" fill="#fff" />
          <path d="M205 247c-14 5-23 13-21 23 7 2 18 0 26-6" fill="#f65a1f" stroke="#d94712" stroke-width="6" stroke-linecap="round" />
          <path d="M286 222c10 15 15 30 13 47M241 264c-4 16-11 27-21 35M223 260c-3 16-9 28-17 36" fill="none" stroke="#ff9718" stroke-width="8" stroke-linecap="round" />
          <path d="M285 220c-7-18-24-29-47-34" fill="none" stroke="#fff48e" stroke-width="8" stroke-linecap="round" />
        </g>
        <g class="float-soft">
          <path d="M179 274h62l-8 44h-46l-8-44Z" fill="#ed2d0c" />
          <path d="M192 274h18v44h-12l-6-44ZM226 274h18l-8 44h-10v-44Z" fill="#fff" />
          <path d="M182 273c4-16 18-24 28-13 7-12 25-8 26 6 13-5 25 11 12 21h-73c-9-5-5-16 7-14Z" fill="#ffd329" />
          <circle cx="197" cy="276" r="8" fill="#fff58a" />
          <circle cx="221" cy="274" r="8" fill="#fff58a" />
        </g>
      </svg>
    `;
  }

  function stakeArt() {
    return `
      <svg class="mascot-svg" viewBox="0 0 420 330" aria-hidden="true">
        <defs>
          <linearGradient id="stakeYellow" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stop-color="#fff05a" />
            <stop offset="1" stop-color="#ffc51d" />
          </linearGradient>
        </defs>
        <g class="bounce-soft">
          <path d="M145 136c-23-54 18-106 77-99 63 8 91 62 59 113 24 27 8 67-66 67-76 0-103-39-70-81Z" fill="url(#stakeYellow)" stroke="#ff9718" stroke-width="8" />
          <circle cx="176" cy="119" r="16" fill="#050505" />
          <circle cx="183" cy="111" r="6" fill="#fff" />
          <circle cx="244" cy="119" r="16" fill="#050505" />
          <circle cx="251" cy="111" r="6" fill="#fff" />
          <path d="M195 141c13 11 32 10 45 0" fill="#f65a1f" stroke="#d94712" stroke-width="8" stroke-linecap="round" />
          <path d="M163 79c11-19 31-29 59-28" fill="none" stroke="#fff48e" stroke-width="8" stroke-linecap="round" />
          <path d="M165 168c15 8 29 8 43 0M235 168c14 8 27 8 39 0" fill="none" stroke="#fff48e" stroke-width="6" stroke-linecap="round" />
        </g>
        <g>
          <path d="M123 176h181v116H123V176Z" fill="#ee8a00" stroke="#a65300" stroke-width="8" />
          <path d="M112 159h202v28H112v-28Z" fill="#d77a00" stroke="#a65300" stroke-width="7" />
          <circle cx="214" cy="231" r="37" fill="#bd6200" stroke="#9a4e00" stroke-width="8" />
          <path d="m201 232 32-17-10 36-10-11-18 9 7-17Z" fill="#ffd49a" />
        </g>
        <g class="float-soft">
          <path d="M78 181c-21 7-34 21-40 43l37 1 21-37-18-7Z" fill="#1d2441" />
          <path d="M64 179c17-10 31 15 13 28-16 12-30-17-13-28Z" fill="#ffbd80" stroke="#e88b4a" stroke-width="5" />
          <rect x="70" y="91" width="56" height="118" rx="13" fill="#55b5ef" stroke="#117fbf" stroke-width="7" />
          <path d="M83 108h31L98 160 83 108Z" fill="none" stroke="#fff" stroke-width="6" stroke-linejoin="round" />
        </g>
        <g class="float-soft-delayed">
          <path d="M338 181c21 7 34 21 40 43l-37 1-21-37 18-7Z" fill="#1d2441" />
          <path d="M352 179c-17-10-31 15-13 28 16 12 30-17 13-28Z" fill="#ffbd80" stroke="#e88b4a" stroke-width="5" />
          <rect x="292" y="91" width="56" height="118" rx="13" fill="#55b5ef" stroke="#117fbf" stroke-width="7" />
          <path d="M305 108h31l-16 52-15-52Z" fill="none" stroke="#fff" stroke-width="6" stroke-linejoin="round" />
        </g>
        <g class="float-soft-delayed">
          <path d="M282 97 325 71c6-3 14-1 18 5l8 13c4 7 1 15-5 19l-43 26-21-37Z" fill="#b85d09" stroke="#853b00" stroke-width="7" />
          <path d="M319 74 340 109M298 89l21 37" stroke="#d97816" stroke-width="6" stroke-linecap="round" />
        </g>
      </svg>
    `;
  }

  function payoutArt() {
    return `
      <svg class="mascot-svg" viewBox="0 0 420 330" aria-hidden="true">
        <defs>
          <linearGradient id="payoutYellow" x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stop-color="#fff05a" />
            <stop offset="1" stop-color="#ffc51d" />
          </linearGradient>
        </defs>
        <g class="bill-fall">
          <rect x="74" y="84" width="39" height="68" rx="3" fill="#13c963" transform="rotate(-18 94 118)" />
          <rect x="81" y="95" width="25" height="45" rx="12" fill="#59ee91" transform="rotate(-18 94 118)" />
        </g>
        <g class="bill-fall delay-1">
          <rect x="287" y="76" width="39" height="68" rx="3" fill="#13c963" transform="rotate(7 306 110)" />
          <rect x="294" y="87" width="25" height="45" rx="12" fill="#59ee91" transform="rotate(7 306 110)" />
        </g>
        <g class="bill-fall delay-2">
          <rect x="322" y="198" width="39" height="68" rx="3" fill="#13c963" transform="rotate(10 342 232)" />
          <rect x="329" y="209" width="25" height="45" rx="12" fill="#59ee91" transform="rotate(10 342 232)" />
        </g>
        <g class="bounce-soft">
          <path d="M111 185c-8-77 35-126 105-122 72 4 113 58 99 134 17 17 26 41 18 65H90c-3-32 4-57 21-77Z" fill="url(#payoutYellow)" stroke="#ff9718" stroke-width="8" />
          <path d="M135 88c27-30 109-31 144-6l-12-43c-41-18-95-17-123 4l-9 45Z" fill="#56200f" stroke="#2a0d05" stroke-width="8" />
          <path d="M118 88c26 23 153 25 191 3 4-3 6-9 3-14-36-27-156-30-200-3-6 3-7 10-2 15 2 0 5 0 8-1Z" fill="#5f230d" stroke="#2a0d05" stroke-width="8" />
          <path d="M159 131c7-14 27-14 34 0M232 131c7-14 27-14 34 0" fill="none" stroke="#050505" stroke-width="16" stroke-linecap="round" />
          <path d="M164 128c4 15 14 28 25 39M237 128c5 16 14 29 25 39" fill="none" stroke="#36e569" stroke-width="7" stroke-linecap="round" />
          <circle cx="181" cy="167" r="14" fill="none" stroke="#fff" stroke-width="6" />
          <circle cx="253" cy="167" r="14" fill="none" stroke="#fff" stroke-width="6" />
          <path d="M172 199c23 25 64 26 91 0" fill="#f65a1f" stroke="#d94712" stroke-width="10" stroke-linecap="round" />
          <path d="M140 145c7-25 28-43 57-52" fill="none" stroke="#fff48e" stroke-width="9" stroke-linecap="round" />
        </g>
      </svg>
    `;
  }
})();
