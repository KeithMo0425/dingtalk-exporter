const viewerState = {
    name: decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || ''),
    conversations: [],
    filtered: [],
    currentCid: '',
    offset: 0,
    limit: 100,
    total: 0,
    filter: 'all',
    keyword: '',
};

function apiGet(url) {
    return fetch(url).then(res => {
        if (!res.ok) {
            return res.json().catch(() => ({})).then(body => {
                throw new Error(body.detail || res.statusText);
            });
        }
        return res.json();
    });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function formatDateTime(value) {
    if (!value) return '';
    return String(value).replace('T', ' ').slice(0, 19);
}

function exportFileUrl(path) {
    const safePath = String(path || '').replace(/^\/+/, '');
    return `/api/export-viewer/${encodeURIComponent(viewerState.name)}/files/${safePath.split('/').map(encodeURIComponent).join('/')}`;
}

function renderContentWithAssets(message) {
    let content = message.content || message.text || `[${message.content_type_name || '消息'}]`;
    let html = escapeHtml(content);

    html = html.replace(/\[图片: ([^\]]+)\]/g, (_, path) => {
        const src = exportFileUrl(path.trim());
        return `<img class="msg-image viewer-inline-image" src="${src}" alt="图片">`;
    });

    html = html.replace(/\[附件: ([^\]|]+)(?: \(([^)]*)\))? \| ([^\]]+)\]/g, (_, name, size, path) => {
        const href = exportFileUrl(path.trim());
        const label = escapeHtml(name.trim());
        const sizeText = size ? ` <span class="msg-file-size">${escapeHtml(size)}</span>` : '';
        return `<a class="viewer-file-link" href="${href}" target="_blank">${label}</a>${sizeText}`;
    });

    const referenced = new Set();
    (content.match(/\[图片: ([^\]]+)\]/g) || []).forEach(marker => {
        const path = marker.replace(/^\[图片: /, '').replace(/\]$/, '').trim();
        referenced.add(path);
    });

    const extraImages = (message.image_export_paths || [])
        .filter(Boolean)
        .filter(path => !referenced.has(path))
        .map(path => `<img class="msg-image viewer-inline-image" src="${exportFileUrl(path)}" alt="图片">`)
        .join('');

    return html + extraImages;
}

function renderConversations() {
    const list = document.getElementById('viewerConvList');
    let filtered = viewerState.conversations;

    if (viewerState.filter !== 'all') {
        filtered = filtered.filter(conv => conv.type === viewerState.filter);
    }
    if (viewerState.keyword) {
        const kw = viewerState.keyword.toLowerCase();
        filtered = filtered.filter(conv =>
            String(conv.display_title || '').toLowerCase().includes(kw) ||
            String(conv.title || '').toLowerCase().includes(kw) ||
            String(conv.last_content || '').toLowerCase().includes(kw)
        );
    }

    viewerState.filtered = filtered;
    document.getElementById('viewerConvCount').textContent = `${filtered.length}/${viewerState.conversations.length}`;

    if (!filtered.length) {
        list.innerHTML = '<div class="viewer-no-results">无匹配会话</div>';
        return;
    }

    list.innerHTML = filtered.map(conv => {
        const title = escapeHtml(conv.display_title || conv.title || conv.conversation_id);
        const type = conv.type === 'group' ? '群' : '单聊';
        const count = conv.message_count || 0;
        const last = escapeHtml(conv.last_content || '');
        const active = conv.conversation_id === viewerState.currentCid ? ' active' : '';
        return `
            <div class="conv-item${active}" data-cid="${escapeHtml(conv.conversation_id)}">
                <div class="conv-title">
                    <span class="conv-type-badge ${conv.type === 'group' ? 'group' : 'single'}">${type}</span>
                    <span>${title}</span>
                </div>
                <div class="conv-subtitle">
                    <span>${count} 条</span>
                    <span>${escapeHtml(conv.last_time_str || '')}</span>
                </div>
                <div class="viewer-last">${last}</div>
            </div>
        `;
    }).join('');

    list.querySelectorAll('.conv-item').forEach(item => {
        item.addEventListener('click', () => selectConversation(item.dataset.cid));
    });
}

async function selectConversation(cid, offset = 0) {
    viewerState.currentCid = cid;
    viewerState.offset = offset;
    renderConversations();

    document.getElementById('viewerEmpty').style.display = 'none';
    document.getElementById('viewerMessageView').style.display = 'flex';
    document.getElementById('viewerMessageList').innerHTML = '<div class="loading">加载消息...</div>';

    const params = new URLSearchParams({
        cid,
        limit: viewerState.limit,
        offset: viewerState.offset,
    });
    const data = await apiGet(`/api/export-viewer/${encodeURIComponent(viewerState.name)}/messages?${params}`);
    viewerState.total = data.total;

    document.getElementById('viewerConvTitle').textContent =
        data.conversation.display_title || data.conversation.title || cid;
    document.getElementById('viewerConvMeta').textContent = `${data.conversation.message_count || 0} 条消息`;
    renderMessages(data.messages || []);
    renderPagination();
}

function renderMessages(messages) {
    const list = document.getElementById('viewerMessageList');
    let lastDate = '';

    list.innerHTML = messages.map(message => {
        const date = message.created_at_str ? message.created_at_str.split(' ')[0] : '';
        const sep = date && date !== lastDate
            ? (lastDate = date, `<div class="msg-date-sep"><span>${escapeHtml(date)}</span></div>`)
            : '';
        const sender = escapeHtml(message.sender_name || String(message.sender_id || ''));
        const time = escapeHtml(message.created_at_str || '');
        const content = renderContentWithAssets(message);
        return `
            ${sep}
            <div class="msg-item">
                <div class="msg-sender">${sender}</div>
                <div class="msg-bubble">${content}</div>
                <div class="msg-time">${time}</div>
            </div>
        `;
    }).join('');

    list.querySelectorAll('.msg-image').forEach(img => {
        img.addEventListener('click', () => openLightbox(img.src));
        img.addEventListener('error', () => {
            img.outerHTML = '<div class="msg-image-placeholder">[图片加载失败]</div>';
        });
    });

    list.scrollTop = list.scrollHeight;
}

function renderPagination() {
    const page = Math.floor(viewerState.offset / viewerState.limit) + 1;
    const pages = Math.max(1, Math.ceil(viewerState.total / viewerState.limit));
    document.getElementById('viewerPageInfo').textContent = `${page}/${pages}`;
    document.getElementById('viewerPrevPage').disabled = viewerState.offset <= 0;
    document.getElementById('viewerNextPage').disabled = viewerState.offset + viewerState.limit >= viewerState.total;
}

function openLightbox(src) {
    document.getElementById('lightboxImg').src = src;
    document.getElementById('lightbox').style.display = 'flex';
}

async function initViewer() {
    document.getElementById('viewerTitle').textContent = viewerState.name || '导出记录';
    const data = await apiGet(`/api/export-viewer/${encodeURIComponent(viewerState.name)}/summary`);
    viewerState.conversations = data.conversations || [];
    document.getElementById('viewerMeta').textContent =
        `${data.total_conversations || 0} 个会话 · ${data.total_messages || 0} 条消息 · ${formatDateTime(data.export_time)}`;
    renderConversations();

    const first = viewerState.conversations[0];
    if (first) {
        selectConversation(first.conversation_id);
    }
}

document.getElementById('viewerSearchInput').addEventListener('input', event => {
    viewerState.keyword = event.target.value.trim();
    renderConversations();
});

document.getElementById('viewerSearchClear').addEventListener('click', () => {
    document.getElementById('viewerSearchInput').value = '';
    viewerState.keyword = '';
    renderConversations();
});

document.querySelectorAll('#viewerTypeTabs .sidebar-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('#viewerTypeTabs .sidebar-tab').forEach(t => t.classList.toggle('active', t === tab));
        viewerState.filter = tab.dataset.filter;
        renderConversations();
    });
});

document.getElementById('viewerPrevPage').addEventListener('click', () => {
    if (!viewerState.currentCid || viewerState.offset <= 0) return;
    selectConversation(viewerState.currentCid, Math.max(0, viewerState.offset - viewerState.limit));
});

document.getElementById('viewerNextPage').addEventListener('click', () => {
    if (!viewerState.currentCid || viewerState.offset + viewerState.limit >= viewerState.total) return;
    selectConversation(viewerState.currentCid, viewerState.offset + viewerState.limit);
});

document.getElementById('lightboxClose').addEventListener('click', () => {
    document.getElementById('lightbox').style.display = 'none';
});

document.getElementById('lightbox').addEventListener('click', event => {
    if (event.target.id === 'lightbox') {
        document.getElementById('lightbox').style.display = 'none';
    }
});

initViewer().catch(error => {
    document.getElementById('viewerMeta').textContent = '加载失败';
    document.getElementById('viewerConvList').innerHTML = `<div class="viewer-no-results">${escapeHtml(error.message)}</div>`;
});
