(function () {
  const api = window.relaydeckDesktop;
  const loadingState = document.getElementById('loading-state');
  const errorState = document.getElementById('error-state');
  const panel = document.getElementById('management-panel');
  const startupDot = document.getElementById('startup-dot');
  const startupLabel = document.getElementById('startup-label');
  const startupDetail = document.getElementById('startup-detail');
  const errorDetail = document.getElementById('error-detail');

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
    startupDot.classList.add(status?.healthy ? 'ok' : 'loading');
    startupLabel.textContent = status?.healthy ? '服务已连接' : '正在连接本地服务';
    startupDetail.textContent = status?.healthy ? '管理台已就绪' : '首次启动可能需要一点时间';
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

  function showPanel(status) {
    setStartup(status);
    setVisible(loadingState, false);
    setVisible(errorState, false);
    if (!panel.src) {
      panel.src = status.managementUrl;
    }
    setVisible(panel, true);
  }

  async function refresh() {
    setVisible(errorState, false);
    setVisible(panel, false);
    setVisible(loadingState, true);
    try {
      const status = await api.getStatus();
      if (status?.healthy) {
        showPanel(status);
      } else {
        showFailure(status?.error);
      }
    } catch (error) {
      showFailure(error?.message);
    }
  }

  document.getElementById('minimize-window').addEventListener('click', () => api.minimizeWindow());
  document.getElementById('close-window').addEventListener('click', () => api.closeWindow());
  document.getElementById('open-logs').addEventListener('click', () => api.openLogs());
  document.getElementById('retry-services').addEventListener('click', refresh);
  document.getElementById('restart-services').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    startupLabel.textContent = '正在重启服务';
    try {
      const status = await api.restartServices();
      status?.healthy ? showPanel(status) : showFailure(status?.error);
    } catch (error) {
      showFailure(error?.message);
    } finally {
      event.currentTarget.disabled = false;
    }
  });
  api.onStatusChanged((status) => status?.healthy ? showPanel(status) : showFailure(status?.error));
  refresh();
}());
