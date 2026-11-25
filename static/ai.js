(function () {
  'use strict';

  var panel = document.getElementById('ai-panel');
  if (!panel) {
    return;
  }

  var api = panel.getAttribute('data-api');
  var historyApi = panel.getAttribute('data-history-api');
  var dayTemplate = panel.getAttribute('data-day-template') || '/day/1970-01-01';
  var messagesEl = document.getElementById('ai-messages');
  var form = document.getElementById('ai-form');
  var input = document.getElementById('ai-input');
  var sendBtn = document.getElementById('ai-send');
  var fileInput = document.getElementById('ai-file');
  var pickBtn = document.getElementById('ai-pick');
  var statusEl = document.getElementById('ai-status');
  var suggestionsEl = document.getElementById('ai-suggestions');
  var attachEl = document.getElementById('ai-attach');
  var attachImg = document.getElementById('ai-attach-img');
  var attachName = document.getElementById('ai-attach-name');
  var attachRemove = document.getElementById('ai-attach-remove');

  var history = [];
  var busy = false;
  var attachment = null;
  var attachmentUrl = '';
  var MAX_IMAGE = 10 * 1024 * 1024;

  function setStatus(text) {
    if (statusEl) {
      statusEl.textContent = text || '';
    }
  }

  function scrollMessages() {
    if (messagesEl) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  function dateLabel(date) {
    var parts = String(date).split('-');
    if (parts.length !== 3) {
      return date;
    }
    return Number(parts[1]) + '月' + Number(parts[2]) + '日';
  }

  function changeText(change) {
    var type = change.type || 'create';
    if (type === 'create') {
      return '已添加 · ' + dateLabel(change.date) + ' · ' + change.line;
    }
    if (type === 'update') {
      return (
        '已修改 · ' + dateLabel(change.date) + ' · ' +
        (change.old_line || '') + ' → ' + change.line
      );
    }
    if (type === 'move') {
      return (
        '已移动 · ' + dateLabel(change.from_date) + ' → ' +
        dateLabel(change.date) + ' · ' + change.line
      );
    }
    if (type === 'delete') {
      return '已删除 · ' + dateLabel(change.date) + ' · ' + change.line;
    }
    if (type === 'shift') {
      var scope = change.scope === 'all' ? '全部日程' : '通用日程';
      return (
        '已整体后移 ' + change.count + ' 条' + scope +
        '（' + dateLabel(change.date) + '之后 ' + change.days + ' 天）'
      );
    }
    if (type === 'insert') {
      return (
        '已插入 · ' + dateLabel(change.date) + ' · ' + change.line +
        '（顺延 ' + change.count + ' 条）'
      );
    }
    return '';
  }

  function appendChanges(list, changes) {
    changes.forEach(function (change) {
      var text = changeText(change);
      if (!text) {
        return;
      }
      var date = change.date;
      if (change.type === 'move') {
        date = change.date;
      }
      if (
        date &&
        (change.type === 'create' ||
          change.type === 'update' ||
          change.type === 'move' ||
          change.type === 'insert')
      ) {
        var link = document.createElement('a');
        link.href = dayTemplate.replace('1970-01-01', date);
        link.textContent = text;
        list.appendChild(link);
      } else {
        var row = document.createElement('span');
        row.className = 'ai-change';
        row.textContent = text;
        list.appendChild(row);
      }
    });
  }

  function addMessage(role, text, options) {
    var opts = options || {};
    var item = document.createElement('div');
    item.className = 'ai-msg ai-msg-' + role + (opts.error ? ' is-error' : '');
    item.textContent = String(text || '');
    var changes = opts.changes || [];
    if (changes.length) {
      var list = document.createElement('div');
      list.className = 'ai-msg-actions';
      appendChanges(list, changes);
      item.appendChild(list);
    } else if (opts.actions && opts.actions.length) {
      var actionList = document.createElement('div');
      actionList.className = 'ai-msg-actions';
      opts.actions.forEach(function (event) {
        var link = document.createElement('a');
        link.href = dayTemplate.replace('1970-01-01', event.date);
        link.textContent = dateLabel(event.date) + ' · ' + event.line;
        actionList.appendChild(link);
      });
      item.appendChild(actionList);
    }
    if (messagesEl) {
      messagesEl.appendChild(item);
      scrollMessages();
    }
    return item;
  }

  function findCell(date) {
    var cells = document.querySelectorAll('.cell');
    for (var i = 0; i < cells.length; i += 1) {
      var href = cells[i].getAttribute('href') || '';
      if (href.slice(-1 * ('/day/' + date).length) === '/day/' + date) {
        return cells[i];
      }
    }
    return null;
  }

  function markAdded(events) {
    (events || []).forEach(function (event) {
      var cell = findCell(event.date);
      if (!cell) {
        return;
      }
      cell.setAttribute('aria-label', event.date + '，有日程');
      var dots = cell.querySelector('.dots');
      if (!dots) {
        dots = document.createElement('span');
        dots.className = 'dots';
        dots.setAttribute('aria-hidden', 'true');
        cell.appendChild(dots);
      }
      var dot = document.createElement('span');
      dot.className = 'dot ' + (event.kind === 'interview' ? 'dot-interview' : 'dot-general');
      dots.appendChild(dot);
    });
  }

  function setCellDots(date, counts) {
    var cell = findCell(date);
    if (!cell) {
      return;
    }
    var old = cell.querySelector('.dots');
    if (old) {
      old.remove();
    }
    var interviews = Number(counts.interview || 0);
    var general = Number(counts.general || 0);
    if (!interviews && !general) {
      cell.setAttribute('aria-label', date);
      return;
    }
    cell.setAttribute('aria-label', date + '，有日程');
    var dots = document.createElement('span');
    dots.className = 'dots';
    dots.setAttribute('aria-hidden', 'true');
    var i;
    for (i = 0; i < interviews; i += 1) {
      var blue = document.createElement('span');
      blue.className = 'dot dot-interview';
      dots.appendChild(blue);
    }
    for (i = 0; i < general; i += 1) {
      var white = document.createElement('span');
      white.className = 'dot dot-general';
      dots.appendChild(white);
    }
    cell.appendChild(dots);
  }

  function applyDots(dots) {
    if (!dots || typeof dots !== 'object') {
      return;
    }
    Object.keys(dots).forEach(function (date) {
      setCellDots(date, dots[date] || {});
    });
  }

  function clearAttachment() {
    if (attachmentUrl) {
      URL.revokeObjectURL(attachmentUrl);
      attachmentUrl = '';
    }
    attachment = null;
    if (attachImg) {
      attachImg.removeAttribute('src');
    }
    if (attachName) {
      attachName.textContent = '';
    }
    if (attachEl) {
      attachEl.hidden = true;
    }
    if (fileInput) {
      fileInput.value = '';
    }
  }

  function setAttachment(file) {
    if (!file) {
      return;
    }
    if (file.type && file.type.indexOf('image/') !== 0) {
      addMessage('assistant', '只支持 PNG / JPG / GIF / WebP 截图。', { error: true });
      return;
    }
    if (file.size > MAX_IMAGE) {
      addMessage('assistant', '截图不能超过 10MB。', { error: true });
      return;
    }
    clearAttachment();
    attachment = file;
    if (window.URL && URL.createObjectURL) {
      attachmentUrl = URL.createObjectURL(file);
      if (attachImg) {
        attachImg.src = attachmentUrl;
      }
    }
    if (attachName) {
      attachName.textContent = file.name || '剪贴板截图';
    }
    if (attachEl) {
      attachEl.hidden = false;
    }
    if (input) {
      input.focus();
    }
  }

  function autoGrow() {
    if (!input) {
      return;
    }
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  }

  function sendMessage() {
    if (busy) {
      return;
    }
    var text = (input && input.value ? input.value : '').trim();
    if (!text && !attachment) {
      if (input) {
        input.focus();
      }
      return;
    }

    var priorHistory = history.slice(-12);
    var displayText = text || '（发送了一张截图）';
    addMessage('user', displayText);
    history.push({ role: 'user', content: text || '（发送了一张截图）' });
    if (input) {
      input.value = '';
      autoGrow();
    }

    var payload = new FormData();
    payload.append('message', text);
    payload.append('history', JSON.stringify(priorHistory));
    if (attachment) {
      payload.append('image', attachment, attachment.name || 'clipboard.png');
    }

    busy = true;
    if (sendBtn) {
      sendBtn.disabled = true;
    }
    setStatus('思考中…');

    fetch(api, { method: 'POST', body: payload })
      .then(function (response) {
        return response.json().catch(function () {
          return { error: '服务返回格式异常（HTTP ' + response.status + '）' };
        }).then(function (data) {
          if (!response.ok) {
            throw new Error(data.error || ('请求失败（HTTP ' + response.status + '）'));
          }
          return data;
        });
      })
      .then(function (data) {
        var reply = data.reply || '已处理。';
        var added = data.added || [];
        var changes = data.changes || [];
        addMessage(
          'assistant',
          reply,
          changes.length ? { changes: changes } : { actions: added }
        );
        if (data.dots) {
          applyDots(data.dots);
        } else {
          markAdded(added);
        }
        history.push({ role: 'assistant', content: reply });
        clearAttachment();
      })
      .catch(function (error) {
        addMessage('assistant', '请求失败：' + error.message, { error: true });
      })
      .then(function () {
        busy = false;
        if (sendBtn) {
          sendBtn.disabled = false;
        }
        setStatus('');
        if (input) {
          input.focus();
        }
      });
  }

  if (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      sendMessage();
    });
  }

  if (input) {
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    });
  }

  if (pickBtn && fileInput) {
    pickBtn.addEventListener('click', function () {
      fileInput.click();
    });
  }

  if (fileInput) {
    fileInput.addEventListener('change', function () {
      if (fileInput.files && fileInput.files.length) {
        setAttachment(fileInput.files[0]);
      }
    });
  }

  if (attachRemove) {
    attachRemove.addEventListener('click', clearAttachment);
  }

  if (suggestionsEl) {
    suggestionsEl.addEventListener('click', function (event) {
      var button = event.target.closest('.ai-suggestion');
      if (!button || busy) {
        return;
      }
      if (input) {
        input.value = button.getAttribute('data-prompt') || button.textContent || '';
        autoGrow();
        sendMessage();
      }
    });
  }

  if (panel) {
    panel.addEventListener('dragover', function (event) {
      event.preventDefault();
    });
    panel.addEventListener('drop', function (event) {
      event.preventDefault();
      if (event.dataTransfer && event.dataTransfer.files.length) {
        setAttachment(event.dataTransfer.files[0]);
      }
    });
  }

  document.addEventListener('paste', function (event) {
    var clipboard = event.clipboardData;
    if (!clipboard || !clipboard.items) {
      return;
    }
    for (var i = 0; i < clipboard.items.length; i += 1) {
      var item = clipboard.items[i];
      if (item.kind === 'file' && item.type && item.type.indexOf('image/') === 0) {
        var file = item.getAsFile();
        if (file) {
          event.preventDefault();
          setAttachment(file);
          return;
        }
      }
    }
  });

  function loadHistory() {
    if (!historyApi || !messagesEl) {
      return;
    }
    fetch(historyApi, { credentials: 'same-origin' })
      .then(function (response) {
        return response.ok ? response.json() : { messages: [] };
      })
      .then(function (data) {
        var messages = data.messages || [];
        if (!messages.length) {
          return;
        }
        messagesEl.textContent = '';
        messages.forEach(function (message) {
          addMessage(message.role, message.content, {
            changes: message.changes || [],
          });
        });
        scrollMessages();
      })
      .catch(function () {
        /* 历史加载失败不影响当前对话 */
      });
  }

  autoGrow();
  loadHistory();
})();
