(function () {
  'use strict';

  var page = document.querySelector('.day-page');
  if (!page) {
    return;
  }

  var date = page.getAttribute('data-date');
  var apiBase = page.getAttribute('data-api') || '/api/days/' + date;
  var imageApiBase = (page.getAttribute('data-image-api') || '/api/images/0').replace(/0$/, '');
  var MAX_UPLOAD = 10 * 1024 * 1024;

  var textarea = document.getElementById('event-input');
  var statusEl = document.getElementById('save-status');
  var shotsStatusEl = document.getElementById('shots-status');
  var zone = document.getElementById('paste-zone');
  var shotsEl = document.getElementById('shots');
  var emptyEl = document.getElementById('shots-empty');
  var fileInput = document.getElementById('file-input');

  var lightbox = document.getElementById('lightbox');
  var lightboxImg = document.getElementById('lightbox-img');
  var lightboxTime = document.getElementById('lightbox-time');
  var lightboxDownload = document.getElementById('lightbox-download');
  var lightboxDelete = document.getElementById('lightbox-delete');
  var lightboxClose = document.getElementById('lightbox-close');
  var activeShot = null;

  /* ---------------- 事件：自动保存 ---------------- */
  var saveTimer = null;
  var pending = false;
  var saving = false;
  var retryTimer = null;

  function setStatus(text, state) {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text || '';
    statusEl.className = 'status' + (state ? ' ' + state : '');
  }

  function setShotsStatus(text, state) {
    if (!shotsStatusEl) {
      return;
    }
    shotsStatusEl.textContent = text || '';
    shotsStatusEl.className = 'status' + (state ? ' ' + state : '');
  }

  function queueSave() {
    pending = true;
    setStatus('未保存', '');
    clearTimeout(saveTimer);
    clearTimeout(retryTimer);
    saveTimer = setTimeout(saveNow, 600);
  }

  function saveNow() {
    clearTimeout(saveTimer);
    if (saving || !pending) {
      return;
    }
    saving = true;
    pending = false;
    setStatus('保存中…', '');
    var value = textarea.value;
    fetch(apiBase, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event: value })
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        if (textarea.value === value) {
          setStatus('已保存' + (data.time_label ? ' ' + data.time_label : ''), 'is-saved');
        } else {
          pending = true;
        }
      })
      .catch(function () {
        pending = true;
        setStatus('保存失败，稍后重试', 'is-error');
        clearTimeout(retryTimer);
        retryTimer = setTimeout(saveNow, 5000);
      })
      .then(function () {
        saving = false;
        if (pending && !retryTimer) {
          saveNow();
        }
      });
  }

  textarea.addEventListener('input', queueSave);
  textarea.addEventListener('blur', saveNow);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      saveNow();
    }
  });
  window.addEventListener('pagehide', function () {
    if (!pending) {
      return;
    }
    try {
      fetch(apiBase, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event: textarea.value }),
        keepalive: true
      });
    } catch (err) {
      /* 忽略离开页面时的保存失败 */
    }
  });

  /* ---------------- 截图 ---------------- */
  function updateEmpty() {
    if (!emptyEl) {
      return;
    }
    emptyEl.hidden = !!shotsEl.querySelector('.shot');
  }

  function formatSize(bytes) {
    if (!bytes && bytes !== 0) {
      return '';
    }
    if (bytes < 1024) {
      return bytes + ' B';
    }
    if (bytes < 1024 * 1024) {
      return Math.round(bytes / 1024) + ' KB';
    }
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function buildShot(image) {
    var figure = document.createElement('figure');
    figure.className = 'shot';
    figure.dataset.id = image.id;
    figure.dataset.src = image.url;
    figure.dataset.download = image.download_url;
    figure.dataset.time = image.time_label;
    figure.title = '点击查看';

    var img = document.createElement('img');
    img.src = image.url;
    img.alt = '截图 ' + image.time_label;
    img.loading = 'lazy';

    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'shot-del';
    remove.textContent = '×';
    remove.title = '删除';
    remove.setAttribute('aria-label', '删除截图');

    var caption = document.createElement('figcaption');
    caption.textContent =
      image.time_label + ' · ' + (image.size_label || formatSize(image.size));

    figure.appendChild(img);
    figure.appendChild(remove);
    figure.appendChild(caption);
    return figure;
  }

  function uploadImage(file) {
    if (!file || !file.type || file.type.indexOf('image/') !== 0) {
      return;
    }
    if (file.size > MAX_UPLOAD) {
      setShotsStatus('截图不能超过 10MB', 'is-error');
      return;
    }
    var placeholder = document.createElement('figure');
    placeholder.className = 'shot is-uploading';
    var label = document.createElement('div');
    label.className = 'shot-loading';
    label.textContent = '上传中…';
    placeholder.appendChild(label);
    shotsEl.appendChild(placeholder);
    updateEmpty();
    setShotsStatus('正在上传截图…', '');

    fetch(apiBase + '/images', {
      method: 'POST',
      headers: { 'Content-Type': file.type },
      body: file
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data && data.error ? data.error : 'HTTP ' + response.status);
          }
          return data;
        });
      })
      .then(function (data) {
        placeholder.remove();
        shotsEl.appendChild(buildShot(data.image));
        updateEmpty();
        setShotsStatus('截图已保存', 'is-saved');
      })
      .catch(function (err) {
        placeholder.remove();
        updateEmpty();
        setShotsStatus('截图上传失败：' + err.message, 'is-error');
      });
  }

  function uploadFiles(fileList) {
    Array.prototype.forEach.call(fileList, uploadImage);
  }

  document.addEventListener('paste', function (event) {
    var clipboard = event.clipboardData;
    if (!clipboard || !clipboard.items) {
      return;
    }
    var files = [];
    for (var i = 0; i < clipboard.items.length; i += 1) {
      var item = clipboard.items[i];
      if (item.kind === 'file' && item.type && item.type.indexOf('image/') === 0) {
        var file = item.getAsFile();
        if (file) {
          files.push(file);
        }
      }
    }
    if (!files.length) {
      return;
    }
    event.preventDefault();
    uploadFiles(files);
  });

  if (zone) {
    zone.addEventListener('click', function (event) {
      if (event.target.closest && event.target.closest('.shot')) {
        return;
      }
      if (fileInput) {
        fileInput.click();
      }
    });
    zone.addEventListener('keydown', function (event) {
      if (
        (event.key === 'Enter' || event.key === ' ') &&
        event.target === zone &&
        fileInput
      ) {
        event.preventDefault();
        fileInput.click();
      }
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.add('is-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        if (name === 'drop' && event.dataTransfer && event.dataTransfer.files.length) {
          uploadFiles(event.dataTransfer.files);
        }
        zone.classList.remove('is-dragover');
      });
    });
  }

  if (fileInput) {
    fileInput.addEventListener('change', function () {
      uploadFiles(fileInput.files);
      fileInput.value = '';
    });
  }

  document.addEventListener('dragover', function (event) {
    event.preventDefault();
  });
  document.addEventListener('drop', function (event) {
    if (!zone || !zone.contains(event.target)) {
      event.preventDefault();
    }
  });

  /* ---------------- 查看 / 删除 ---------------- */
  function openLightbox(figure) {
    activeShot = { id: figure.dataset.id, figure: figure };
    lightboxImg.src = figure.dataset.src;
    lightboxTime.textContent = '截图 ' + figure.dataset.time;
    lightboxDownload.href = figure.dataset.download;
    lightbox.hidden = false;
    document.body.classList.add('no-scroll');
    lightboxClose.focus();
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.removeAttribute('src');
    document.body.classList.remove('no-scroll');
    activeShot = null;
    if (textarea) {
      textarea.focus();
    }
  }

  function deleteShot(figure) {
    if (!window.confirm('删除这张截图？')) {
      return;
    }
    fetch(imageApiBase + figure.dataset.id, { method: 'DELETE' })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        if (activeShot && activeShot.figure === figure) {
          closeLightbox();
        }
        figure.remove();
        updateEmpty();
        setShotsStatus('截图已删除', 'is-saved');
      })
      .catch(function (err) {
        window.alert('删除失败：' + err.message);
      });
  }

  shotsEl.addEventListener('click', function (event) {
    var figure = event.target.closest ? event.target.closest('.shot') : null;
    if (!figure) {
      return;
    }
    if (event.target.closest('.shot-del')) {
      event.preventDefault();
      event.stopPropagation();
      deleteShot(figure);
      return;
    }
    openLightbox(figure);
  });

  lightboxClose.addEventListener('click', closeLightbox);
  lightbox.addEventListener('click', function (event) {
    if (event.target === lightbox) {
      closeLightbox();
    }
  });
  lightboxDelete.addEventListener('click', function () {
    if (activeShot) {
      deleteShot(activeShot.figure);
    }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !lightbox.hidden) {
      closeLightbox();
    }
  });

  updateEmpty();
})();
