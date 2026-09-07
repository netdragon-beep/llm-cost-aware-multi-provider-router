(function () {
  const api = window.relaydeckDesktop;
  const loadingState = document.getElementById('loading-state');
  const errorState = document.getElementById('error-state');
  const panel = document.getElementById('management-panel');
  const startupDot = document.getElementById('startup-dot');
  const startupLabel = document.getElementById('startup-label');
  const startupDetail = document.getElementById('startup-detail');
  const errorDetail = document.getElementById('error-detail');
  let managementPanelRevision = 0;

  function setVisible(element, visible) {
    element.hidden = !visible;
  }

  function setStartup(status) {
    const services = status?.services || {};
    document.querySelectorAll('[data-service]').forEach((item) => {
      const dot = item.querySelector('.dot');
      dot.classList.remove('ok', 'bad', 'loading');
      dot.classList.add(services[item.dataset.service] ? 'ok' : 'bad');
    });
    startupDot.classList.remove('ok', 'bad', 'loading');
    const managementHealthy = Boolean(status?.managementHealthy ?? status?.healthy);
    const stackHealthy = Boolean(status?.stackHealthy);
    startupDot.classList.add(stackHealthy ? 'ok' : managementHealthy ? 'bad' : 'loading');
    startupLabel.textContent = stackHealthy ? '核心服务已连接' : managementHealthy ? '部分服务未启动' : '正在连接本地服务';
    startupDetail.textContent = stackHealthy ? '管理台与网关已就绪' : managementHealthy ? '请查看右侧服务指示灯' : '首次启动可能需要一点时间';
  }

  function showFailure(message) {
    setVisible(loadingState, false);
    setVisible(panel, false);
    setVisible(errorState, true);
    startupDot.classList.remove('loading', 'ok');
    startupDot.classList.add('bad');
    startupLabel.textContent = '服务未连接';
    startupDetail.textContent = '请检查诊断信息';
    errorDetail.textContent = message || '请检查本地服务状态后重试。';
  }

  function managementPanelUrl(managementUrl) {
    if (typeof managementUrl !== 'string' || !managementUrl) {
      return '';
    }
    const cacheBustSeparator = managementUrl.includes('?') ? '&' : '?';
    managementPanelRevision += 1;
    return `${managementUrl}${cacheBustSeparator}_relaydeck=${Date.now()}-${managementPanelRevision}`;
  }

  function showPanel(status, { forceReload = false } = {}) {
    setStartup(status);
    setVisible(loadingState, false);
    setVisible(errorState, false);
    if (forceReload || !panel.getAttribute('src')) {
      const panelUrl = managementPanelUrl(status?.managementUrl);
      if (panelUrl) {
        panel.src = panelUrl;
      }
    }
    setVisible(panel, true);
  }

  async function refresh({ forceReload = true } = {}) {
    setVisible(errorState, false);
    setVisible(panel, false);
    setVisible(loadingState, true);
    try {
      const status = await api.getStatus();
      if (status?.healthy) {
        showPanel(status, { forceReload });
      } else {
        showFailure(status?.error);
      }
    } catch (error) {
      showFailure(error?.message);
    }
  }

  async function refreshStatusOnly() {
    try {
      const status = await api.getStatus();
      setStartup(status);
      if (status?.managementHealthy && panel.hidden) {
        showPanel(status, { forceReload: true });
      }
    } catch {
      setStartup({ services: {}, managementHealthy: false, stackHealthy: false });
    }
  }

  document.getElementById('minimize-window').addEventListener('click', () => api.minimizeWindow());
  document.getElementById('close-window').addEventListener('click', () => api.closeWindow());
  document.getElementById('open-logs').addEventListener('click', () => api.openLogs());
  document.getElementById('retry-services').addEventListener('click', () => refresh({ forceReload: true }));
  document.getElementById('restart-services').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    startupLabel.textContent = '正在重启服务';
    try {
      const status = await api.restartServices();
      status?.healthy ? showPanel(status, { forceReload: true }) : showFailure(status?.error);
    } catch (error) {
      showFailure(error?.message);
    } finally {
      event.currentTarget.disabled = false;
    }
  });
  api.onStatusChanged((status) => {
    if (status?.healthy) {
      showPanel(status, { forceReload: panel.hidden });
    } else {
      showFailure(status?.error);
    }
  });
  refresh();
  setInterval(refreshStatusOnly, 3000);
}());
