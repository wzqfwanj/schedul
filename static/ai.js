(function () {
  'use strict';

  var panel = document.getElementById('ai-panel');
  if (!panel) {
    return;
  }

  var api = panel.getAttribute('data-api');
  var dayTemplate = panel.getAttribute('data-day-template') || '/day/1970-01-01';
  var drop = document.getElementById('ai-drop');
  var fileInput = document.getElementById('ai-file');
  var statusEl = document.getElementById('ai-status');
  var resultEl = document.getElementById('ai-result');
  var previewEl = document.getElementById('ai-preview');

  var busy = false;
  var previewUrl = '';

  function setStatus(text, state) {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text || '';
    statusEl.className = 'status' + (state ? ' ' + state : '');
  }

  function clearPreview() {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = '';
    }
    if (previewEl) {
      previewEl.hidden = true;
      previewEl.removeAttribute('src');
    }
  }

  function showPreview(file) {
    clearPreview();
    if (!previewEl || !window.URL || !URL.createObjectURL) {
      return;
    }
    previewUrl = URL.createObjectURL(file);
    previewEl.src = previewUrl;
    previewEl.hidden = false;
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

  function markDate(date) {
    var cell = findCell(date);
    if (!cell) {
      return;
    }
    cell.setAttribute('aria-label', date + '，有日程');
    if (!cell.querySelector('.dot')) {
      var dot = document.createElement('span');
      dot.className = 'dot';
      dot.setAttribute('aria-hidden', 'true');
      cell.appendChild(dot);
    }
  }

  function dateLabel(date) {
    var parts = String(date).split('-');
    if (parts.length !== 3) {
      return date;
    }
    return Number(parts[1]) + '月' + Number(parts[2]) + '日';
  }

  function makeItem(event, skipped) {
    var link = document.createElement('a');
    link.className = 'ai-item' + (skipped ? ' is-skipped' : '');
    link.href = dayTemplate.replace('1970-01-01', event.date);
    link.title = skipped ? '这条已经存在，未重复添加' : '打开当天日程';

    var head = document.createElement('span');
    head.className = 'ai-item-head';

    var date = document.createElement('span');
    date.className = 'ai-date';
    date.textContent = dateLabel(event.date);

    var time = document.createElement('span');
    time.className = 'ai-time' + (event.time ? '' : ' is-unset');
    time.textContent = event.time || '未识别时间';

    var isInterview = event.kind === 'interview';
    var kind = document.createElement('span');
    kind.className = 'ai-kind';
    kind.textContent = isInterview ? '面试' : '日程';

    head.appendChild(date);
    head.appendChild(time);
    head.appendChild(kind);
    if (skipped) {
      var skip = document.createElement('span');
      skip.className = 'ai-skip';
      skip.textContent = '已存在';
      head.appendChild(skip);
    }

    var fields = document.createElement('span');
    fields.className = 'ai-fields';
    var rows = [];
    if (!isInterview) {
      if (event.title) {
        rows.push('事项：' + event.title);
      }
      if (event.address) {
        rows.push('地址：' + event.address);
      }
      if (!rows.length && event.company) {
        rows.push('公司：' + event.company);
      }
      if (!rows.length && event.role) {
        rows.push('岗位：' + event.role);
      }
    } else {
      if (event.company) {
        rows.push('公司：' + event.company);
      }
      if (event.role) {
        rows.push('岗位：' + event.role);
      }
      if (event.address) {
        rows.push('地址：' + event.address);
      }
      if (!rows.length && event.title) {
        rows.push(event.title);
      }
    }
    if (!rows.length && event.note) {
      rows.push(event.note);
    }
    rows.forEach(function (text) {
      var field = document.createElement('span');
      field.className = 'ai-field';
      field.textContent = text;
      fields.appendChild(field);
    });

    link.appendChild(head);
    link.appendChild(fields);
    return link;
  }

  function renderError(message) {
    resultEl.textContent = '';
    resultEl.hidden = false;
    var text = document.createElement('p');
    text.className = 'ai-err';
    text.textContent = message;
    resultEl.appendChild(text);
  }

  function renderResult(data) {
    var added = data.added || [];
    var skipped = data.skipped || [];
    resultEl.textContent = '';

    if (!added.length && !skipped.length) {
      resultEl.hidden = false;
      var empty = document.createElement('p');
      empty.className = 'ai-empty';
      empty.textContent = '没有识别到明确日程，换一张更清晰的截图试试。';
      resultEl.appendChild(empty);
      return;
    }

    resultEl.hidden = false;
    var head = document.createElement('p');
    head.className = 'ai-result-head';
    if (added.length) {
      head.textContent = '已添加 ' + added.length + ' 条日程到日历';
    } else {
      head.textContent = '这些日程已经存在，没有重复添加';
    }
    resultEl.appendChild(head);

    added.forEach(function (event) {
      resultEl.appendChild(makeItem(event, false));
      markDate(event.date);
    });
    skipped.forEach(function (event) {
      resultEl.appendChild(makeItem(event, true));
    });

    var outside = 0;
    added.concat(skipped).forEach(function (event) {
      if (!findCell(event.date)) {
        outside += 1;
      }
    });
    var noTime = added.filter(function (event) {
      return !event.time;
    }).length;
    if (noTime) {
      var timeNote = document.createElement('p');
      timeNote.className = 'ai-more';
      timeNote.textContent = '有 ' + noTime + ' 条没有识别到具体时间，可以点进去补充。';
      resultEl.appendChild(timeNote);
    }
    if (outside) {
      var more = document.createElement('p');
      more.className = 'ai-more';
      more.textContent = '有 ' + outside + ' 条不在当前月份，已写入日历，切换月份可看到标记。';
      resultEl.appendChild(more);
    }
  }

  function parseImage(file) {
    if (busy) {
      return;
    }
    if (!file) {
      return;
    }
    if (file.type && file.type.indexOf('image/') !== 0) {
      renderError('只支持粘贴图片截图。');
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      renderError('截图不能超过 10MB。');
      return;
    }

    busy = true;
    drop.classList.add('is-busy');
    drop.setAttribute('aria-busy', 'true');
    setStatus('识别中…', '');
    resultEl.hidden = true;
    resultEl.textContent = '';
    showPreview(file);

    fetch(api, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (!response.ok) {
              throw new Error(data.error || '识别失败（HTTP ' + response.status + '）');
            }
            return data;
          });
      })
      .then(function (data) {
        renderResult(data);
        setStatus('完成', 'is-saved');
      })
      .catch(function (err) {
        renderError(err.message || '识别失败，请稍后重试。');
        setStatus('识别失败', 'is-error');
      })
      .finally(function () {
        busy = false;
        drop.classList.remove('is-busy');
        drop.removeAttribute('aria-busy');
      });
  }

  function pickFile() {
    if (fileInput) {
      fileInput.click();
    }
  }

  if (drop) {
    drop.addEventListener('click', pickFile);
    drop.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        pickFile();
      }
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.add('is-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        if (name === 'drop' && event.dataTransfer && event.dataTransfer.files.length) {
          parseImage(event.dataTransfer.files[0]);
        }
        drop.classList.remove('is-dragover');
      });
    });
  }

  if (fileInput) {
    fileInput.addEventListener('change', function () {
      if (fileInput.files && fileInput.files.length) {
        parseImage(fileInput.files[0]);
      }
      fileInput.value = '';
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
          parseImage(file);
          return;
        }
      }
    }
  });

  document.addEventListener('dragover', function (event) {
    event.preventDefault();
  });
  document.addEventListener('drop', function (event) {
    if (!panel.contains(event.target)) {
      event.preventDefault();
    }
  });
})();
