(function () {
  'use strict';

  var panel = document.getElementById('ai-panel');
  if (!panel) {
    return;
  }

  var api = panel.getAttribute('data-api');
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

  function addMessage(role, text, options) {
    var opts = options || {};
    var item = document.createElement('div');
    item.className = 'ai-msg ai-msg-' + role + (opts.error ? ' is-error' : '');
    item.textContent = String(text || '');
    if (opts.actions && opts.actions.length) {
      var list = document.createElement('div');
      list.className = 'ai-msg-actions';
      opts.actions.forEach(function (event) {
        var link = document.createElement('a');
        link.href = dayTemplate.replace('1970-01-01', event.date);
        link.textContent = dateLabel(event.date) + ' · ' + event.line;
        list.appendChild(link);
      });
      item.appendChild(list);
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
        addMessage('assistant', reply, { actions: added });
        markAdded(added);
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

  autoGrow();
})();
