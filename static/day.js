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

  var editor = document.getElementById('event-editor');
  var initialEvent = editor ? editor.textContent || '' : '';
  var statusEl = document.getElementById('save-status');
  var insertBtn = document.getElementById('insert-data');
  var orphanBar = document.getElementById('orphan-bar');
  var orphanChips = document.getElementById('orphan-chips');

  var dataModal = document.getElementById('data-modal');
  var dataTitle = document.getElementById('data-modal-title');
  var dataClose = document.getElementById('data-close');
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

  var imagesBySlot = {};
  var currentSlot = 0;
  var currentTokenCount = 0;
  var modalOpener = null;

  /* ---------------- 状态提示 ---------------- */
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

  function syncScrollLock() {
    var locked = !dataModal.hidden || !lightbox.hidden;
    document.body.classList.toggle('no-scroll', locked);
  }

  /* ---------------- 事件：序列化 / 保存 ---------------- */
  function serializeEvent() {
    var out = '';
    function walk(node) {
      if (node.nodeType === 3) {
        out += node.nodeValue;
        return;
      }
      if (node.nodeType !== 1) {
        return;
      }
      if (node.classList && node.classList.contains('data-chip')) {
        out += '<资料>';
        return;
      }
      if (node.tagName === 'BR') {
        out += '\n';
        return;
      }
      if (node.tagName === 'DIV' || node.tagName === 'P') {
        if (out && out.charAt(out.length - 1) !== '\n') {
          out += '\n';
        }
        for (var i = 0; i < node.childNodes.length; i += 1) {
          walk(node.childNodes[i]);
        }
        if (out && out.charAt(out.length - 1) !== '\n') {
          out += '\n';
        }
        return;
      }
      for (var j = 0; j < node.childNodes.length; j += 1) {
        walk(node.childNodes[j]);
      }
    }
    if (editor) {
      for (var i = 0; i < editor.childNodes.length; i += 1) {
        walk(editor.childNodes[i]);
      }
    }
    return out;
  }

  var saveTimer = null;
  var pending = false;
  var saving = false;
  var retryTimer = null;

  function queueSave() {
    pending = true;
    setStatus('未保存', '');
    clearTimeout(saveTimer);
    clearTimeout(retryTimer);
    retryTimer = null;
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
    var value = serializeEvent();
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
        if (serializeEvent() === value) {
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

  /* ---------------- 资料标签与分组 ---------------- */
  function imagesIn(slot) {
    return imagesBySlot[slot] || [];
  }

  function chipLabel(slot) {
    var count = imagesIn(slot).length;
    return count ? '资料 ' + count : '资料';
  }

  function createChip() {
    var chip = document.createElement('span');
    chip.className = 'data-chip';
    chip.contentEditable = 'false';
    chip.setAttribute('role', 'button');
    chip.setAttribute('tabindex', '0');
    chip.textContent = '资料';
    chip.title = '点击打开这组资料截图';
    chip.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      openDataModal(Number(chip.dataset.slot), chip);
    });
    chip.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDataModal(Number(chip.dataset.slot), chip);
      }
    });
    return chip;
  }

  function orphanImages() {
    var result = [];
    Object.keys(imagesBySlot).forEach(function (key) {
      if (Number(key) >= currentTokenCount) {
        result = result.concat(imagesBySlot[key]);
      }
    });
    return result;
  }

  function renderOrphans() {
    if (!orphanBar || !orphanChips) {
      return;
    }
    var orphans = orphanImages();
    orphanChips.textContent = '';
    orphanBar.hidden = orphans.length === 0;
    if (!orphans.length) {
      return;
    }
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'data-chip data-chip-orphan';
    chip.textContent = '未归类资料 ' + orphans.length;
    chip.title = '这些截图还没有对应的 <资料>；在事件里加一个 <资料> 即可归入';
    chip.addEventListener('click', function () {
      openDataModal('orphan', chip);
    });
    orphanChips.appendChild(chip);
  }

  function refreshChips() {
    if (!editor) {
      return;
    }
    var chips = editor.querySelectorAll('.data-chip');
    currentTokenCount = chips.length;
    for (var i = 0; i < chips.length; i += 1) {
      chips[i].dataset.slot = String(i);
      chips[i].textContent = chipLabel(i);
      chips[i].title = '点击打开这组资料截图';
    }
    renderOrphans();
  }

  function renderEventContent(text) {
    editor.textContent = '';
    var slot = 0;
    var lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
    lines.forEach(function (line, lineIndex) {
      if (lineIndex > 0) {
        editor.appendChild(document.createElement('br'));
      }
      line.split('<资料>').forEach(function (part, index) {
        if (index > 0) {
          editor.appendChild(createChip());
          slot += 1;
        }
        if (part) {
          editor.appendChild(document.createTextNode(part));
        }
      });
    });
    refreshChips();
  }

  function convertAllTokens() {
    if (!editor) {
      return;
    }
    var walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) {
      nodes.push(walker.currentNode);
    }
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      if (text.indexOf('<资料>') === -1) {
        return;
      }
      var parts = text.split('<资料>');
      var fragment = document.createDocumentFragment();
      parts.forEach(function (part, index) {
        if (index > 0) {
          fragment.appendChild(createChip());
        }
        if (part) {
          fragment.appendChild(document.createTextNode(part));
        }
      });
      node.parentNode.replaceChild(fragment, node);
    });
    refreshChips();
  }

  function convertTokenAtCaret() {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount || !sel.isCollapsed) {
      return;
    }
    var node = sel.anchorNode;
    if (!node || node.nodeType !== 3 || !editor.contains(node)) {
      return;
    }
    var offset = sel.anchorOffset;
    var text = node.nodeValue;
    if (offset < 4 || text.slice(offset - 4, offset) !== '<资料>') {
      return;
    }
    var parentNode = node.parentNode;
    node.nodeValue = text.slice(0, offset - 4);
    var chip = createChip();
    var afterNode = document.createTextNode(text.slice(offset));
    parentNode.insertBefore(afterNode, node.nextSibling);
    parentNode.insertBefore(chip, afterNode);
    if (!node.nodeValue) {
      parentNode.removeChild(node);
    }
    var range = document.createRange();
    range.setStartAfter(chip);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
    refreshChips();
  }

  function insertChipAtCaret() {
    if (!editor) {
      return;
    }
    editor.focus();
    var sel = window.getSelection();
    var range;
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      range = sel.getRangeAt(0);
    } else {
      range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    range.deleteContents();
    var chip = createChip();
    range.insertNode(chip);
    var newRange = document.createRange();
    newRange.setStartAfter(chip);
    newRange.collapse(true);
    sel.removeAllRanges();
    sel.addRange(newRange);
    refreshChips();
    queueSave();
  }

  /* ---------------- 事件：自动保存事件 ---------------- */
  editor.addEventListener('input', function () {
    queueSave();
    convertTokenAtCaret();
    window.setTimeout(refreshChips, 0);
  });
  editor.addEventListener('blur', function () {
    convertAllTokens();
    saveNow();
  });
  editor.addEventListener('paste', function (event) {
    if (event.clipboardData && event.clipboardData.files && event.clipboardData.files.length) {
      return; // 图片粘贴走弹窗里的上传逻辑
    }
    var text = event.clipboardData ? event.clipboardData.getData('text/plain') : '';
    if (!text) {
      return;
    }
    event.preventDefault();
    var parts = text.replace(/\r\n/g, '\n').split('\n');
    parts.forEach(function (part, index) {
      if (index > 0) {
        document.execCommand('insertLineBreak');
      }
      if (part) {
        document.execCommand('insertText', false, part);
      }
    });
    queueSave();
  });
  editor.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      document.execCommand('insertLineBreak');
      queueSave();
    }
  });
  insertBtn.addEventListener('click', insertChipAtCaret);

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
        body: JSON.stringify({ event: serializeEvent() }),
        keepalive: true
      });
    } catch (err) {
      /* 忽略离开页面时的保存失败 */
    }
  });

  /* ---------------- 资料弹窗 ---------------- */
  function openDataModal(slot, opener) {
    currentSlot = slot;
    modalOpener = opener || null;
    dataTitle.textContent = slot === 'orphan' ? '未归类资料' : '资料';
    renderShots();
    dataModal.hidden = false;
    syncScrollLock();
    dataClose.focus();
  }

  function closeDataModal() {
    if (dataModal.hidden) {
      return;
    }
    dataModal.hidden = true;
    syncScrollLock();
    if (modalOpener && document.contains(modalOpener)) {
      modalOpener.focus();
    }
    modalOpener = null;
  }

  function renderShots() {
    var list = currentSlot === 'orphan' ? orphanImages() : imagesIn(currentSlot);
    shotsEl.textContent = '';
    list.forEach(function (image) {
      shotsEl.appendChild(buildShot(image));
    });
    emptyEl.hidden = list.length > 0;
    setShotsStatus('', '');
  }

  function refreshAfterImagesChange() {
    refreshChips();
    if (!dataModal.hidden) {
      renderShots();
    }
  }

  dataClose.addEventListener('click', closeDataModal);
  dataModal.addEventListener('click', function (event) {
    if (event.target === dataModal) {
      closeDataModal();
    }
  });

  /* ---------------- 截图 ---------------- */
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
    figure.dataset.slot = image.slot;
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
    if (dataModal.hidden) {
      return;
    }
    if (file.size > MAX_UPLOAD) {
      setShotsStatus('截图不能超过 10MB', 'is-error');
      return;
    }
    var targetSlot = currentSlot === 'orphan' ? currentTokenCount : Number(currentSlot);
    var placeholder = document.createElement('figure');
    placeholder.className = 'shot is-uploading';
    var label = document.createElement('div');
    label.className = 'shot-loading';
    label.textContent = '上传中…';
    placeholder.appendChild(label);
    shotsEl.appendChild(placeholder);
    emptyEl.hidden = true;
    setShotsStatus('正在上传截图…', '');

    fetch(apiBase + '/images?slot=' + targetSlot, {
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
        if (!imagesBySlot[targetSlot]) {
          imagesBySlot[targetSlot] = [];
        }
        imagesBySlot[targetSlot].push(data.image);
        refreshAfterImagesChange();
        setShotsStatus('截图已保存', 'is-saved');
      })
      .catch(function (err) {
        placeholder.remove();
        refreshAfterImagesChange();
        setShotsStatus('截图上传失败：' + err.message, 'is-error');
      });
  }

  function uploadFiles(fileList) {
    Array.prototype.forEach.call(fileList, uploadImage);
  }

  document.addEventListener('paste', function (event) {
    if (dataModal.hidden) {
      return;
    }
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
    syncScrollLock();
    lightboxClose.focus();
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.removeAttribute('src');
    syncScrollLock();
    activeShot = null;
    if (!dataModal.hidden) {
      dataClose.focus();
    } else if (editor) {
      editor.focus();
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
        var imageId = Number(figure.dataset.id);
        Object.keys(imagesBySlot).forEach(function (key) {
          imagesBySlot[key] = imagesBySlot[key].filter(function (image) {
            return Number(image.id) !== imageId;
          });
        });
        figure.remove();
        refreshAfterImagesChange();
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
    if (event.key !== 'Escape') {
      return;
    }
    if (!lightbox.hidden) {
      closeLightbox();
      return;
    }
    if (!dataModal.hidden) {
      closeDataModal();
    }
  });

  /* ---------------- 初始化 ---------------- */
  renderEventContent(initialEvent);
  updateImagesFromServer();

  function updateImagesFromServer() {
    fetch(apiBase, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        imagesBySlot = {};
        (data.images || []).forEach(function (image) {
          var slot = typeof image.slot === 'number' ? image.slot : 0;
          if (!imagesBySlot[slot]) {
            imagesBySlot[slot] = [];
          }
          imagesBySlot[slot].push(image);
        });
        refreshChips();
        if (!dataModal.hidden) {
          renderShots();
        }
      })
      .catch(function () {
        /* 读取失败时保持空状态，不阻塞页面 */
      });
  }
})();
