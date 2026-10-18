(() => {
    const charts = document.getElementById('charts-view');
    const archive = document.getElementById('editors-view');
    const preview = document.getElementById('editor-preview');
    const previewYear = document.getElementById('preview-year');
    const archiveYear = document.getElementById('editor-year');
    const message = document.getElementById('editor-message');
    const retry = document.getElementById('editor-retry');
    const previewTracks = document.getElementById('preview-tracks');
    const motionButton = document.getElementById('preview-motion');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let editions = [];
    let selectedYear;
    let chartScroll = 0;
    let previewPosition = 0;
    let previewDistance = 0;
    let previewVisible = false;
    let motionPaused = false;
    let dragging = false;
    let pauseUntil = 0;
    let lastFrame = 0;

    function measurePreview() {
        previewDistance = previewTracks.querySelector('.editor-preview-group')?.offsetWidth || 0;
    }

    new IntersectionObserver(([entry]) => {
        previewVisible = entry.isIntersecting;
        if (previewVisible) {
            measurePreview();
            pauseUntil = Math.max(pauseUntil, performance.now() + 1200);
        }
    }).observe(previewTracks);

    function flowPreview(now) {
        const elapsed = lastFrame ? Math.min(now - lastFrame, 50) : 0;
        lastFrame = now;
        if (previewVisible && !charts.hidden && !document.hidden && !motionPaused && !reducedMotion.matches
            && !dragging && !previewTracks.matches(':hover, :focus-within') && now > pauseUntil
            && previewDistance > previewTracks.clientWidth) {
            previewPosition = (previewPosition + elapsed * 0.032) % previewDistance;
            previewTracks.scrollLeft = previewPosition;
        }
        requestAnimationFrame(flowPreview);
    }

    motionButton.addEventListener('click', () => {
        motionPaused = !motionPaused;
        previewPosition = previewTracks.scrollLeft;
        pauseUntil = 0;
        motionButton.textContent = motionPaused ? '▶' : 'Ⅱ';
        motionButton.setAttribute('aria-pressed', String(!motionPaused));
        motionButton.title = motionPaused ? 'Play Auto-Flow' : 'Pause Auto-Flow';
    });
    previewTracks.addEventListener('pointerdown', () => { dragging = true; });
    const releasePreview = () => { dragging = false; };
    window.addEventListener('pointerup', releasePreview);
    window.addEventListener('pointercancel', releasePreview);
    previewTracks.addEventListener('scroll', () => {
        if (Math.abs(previewTracks.scrollLeft - previewPosition) > 2) {
            previewPosition = previewTracks.scrollLeft;
            pauseUntil = performance.now() + 2500;
        }
    }, {passive: true});
    requestAnimationFrame(flowPreview);

    function node(tag, className, text) {
        const result = document.createElement(tag);
        result.className = className;
        if (text !== undefined) result.textContent = text;
        return result;
    }

    function externalLink(label, url) {
        const link = node('a', '', label);
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        return link;
    }

    function validate(data) {
        const years = new Set();
        if (!Array.isArray(data.editions) || !data.editions.length) throw new Error('No editions');
        for (const edition of data.editions) {
            if (!Number.isInteger(edition.year) || edition.year < 2000 || edition.year > 2100 || years.has(edition.year)
                || !['final', 'in_progress'].includes(edition.status)
                || !(edition.published_at === null || (typeof edition.published_at === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(edition.published_at)))
                || !Array.isArray(edition.tracks) || !edition.tracks.length) throw new Error('Invalid edition');
            years.add(edition.year);
            const source = edition.source_url == null ? null : new URL(edition.source_url);
            const playlist = new URL(edition.playlist_url);
            if ((source && (source.protocol !== 'https:' || source.hostname !== 'brunch.co.kr'))
                || playlist.protocol !== 'https:' || !['www.youtube.com', 'music.youtube.com'].includes(playlist.hostname)
                || playlist.pathname !== '/playlist' || !/^PL[\w-]+$/.test(playlist.searchParams.get('list') || '')) throw new Error('Invalid source');
            if (edition.spotify_url != null) {
                const spotify = new URL(edition.spotify_url);
                if (spotify.origin !== 'https://open.spotify.com' || !/^\/playlist\/[A-Za-z0-9]{22}$/.test(spotify.pathname)) throw new Error('Invalid Spotify playlist');
            }
            if (edition.melon_url != null) {
                const melon = new URL(edition.melon_url);
                if (melon.origin !== 'https://www.melon.com' || melon.pathname !== '/mymusic/playlist/mymusicplaylistview_inform.htm'
                    || !/^\d+$/.test(melon.searchParams.get('plylstSeq') || '')) throw new Error('Invalid Melon playlist');
            }
            for (const track of edition.tracks) {
                if (typeof track.title !== 'string' || !track.title.trim() || typeof track.artist !== 'string' || !track.artist.trim()
                    || !(track.reason == null || (typeof track.reason === 'string' && track.reason.trim()))
                    || !Array.isArray(track.videos) || track.videos.some(video => !/^[\w-]{11}$/.test(video.id) || typeof video.label !== 'string')) throw new Error('Invalid track');
            }
        }
        return data.editions.slice().sort((a, b) => b.year - a.year);
    }

    function cover(track) {
        const art = node('span', 'editor-cover', track.title[0]);
        art.setAttribute('aria-hidden', 'true');
        if (track.videos.length) {
            const image = document.createElement('img');
            image.alt = '';
            image.loading = 'lazy';
            image.src = `https://i.ytimg.com/vi/${track.videos[0].id}/mqdefault.jpg`;
            image.addEventListener('error', () => image.remove());
            art.append(image);
        }
        return art;
    }

    function renderEdition() {
        const edition = editions.find(item => item.year === selectedYear);
        if (!edition) return;
        const path = `#editors-choice-${edition.year}`;
        previewYear.value = archiveYear.value = String(edition.year);
        document.getElementById('editors-link').href = path;
        const viewAll = document.getElementById('editor-view-all');
        viewAll.href = path;
        viewAll.textContent = 'View All →';
        viewAll.setAttribute('aria-label', `${edition.year} year's selection · ${edition.tracks.length} tracks`);
        const status = edition.status === 'final' ? 'FINAL' : 'IN PROGRESS';
        document.getElementById('preview-status').textContent = status;
        document.getElementById('editor-status').textContent = status;
        document.getElementById('editor-year-display').textContent = edition.year;
        document.getElementById('editor-meta').textContent = `${edition.tracks.length} tracks selected · editor's compilation · ${edition.published_at ? `${edition.published_at} published` : 'Playlist archive'}`;
        const actions = document.getElementById('editor-actions');
        actions.replaceChildren(externalLink('YouTube Music ↗', edition.playlist_url));
        if (edition.spotify_url) actions.append(externalLink('Spotify ↗', edition.spotify_url));
        if (edition.melon_url) actions.append(externalLink('Melon ↗', edition.melon_url));
        if (edition.source_url) actions.append(externalLink('Read Original Article ↗', edition.source_url));
        const orderedTracks = [...edition.tracks.filter(track => track.highlight), ...edition.tracks.filter(track => !track.highlight)];
        const teasers = orderedTracks.map(track => {
            const link = node('a', `editor-preview-track${track.highlight ? ' is-highlight' : ''}`);
            link.href = path;
            link.setAttribute('aria-label', `${track.highlight ? "This Year's Pick · " : ""}${track.title} · ${track.artist} ${'reason' in track ? 'Selection Reason' : 'View Selection'}`);
            const text = node('span', '');
            text.append(node('strong', '', track.title), node('small', '', track.artist));
            link.append(cover(track), text);
            return link;
        });
        const group = node('div', 'editor-preview-group');
        group.append(...teasers);
        const copy = group.cloneNode(true);
        copy.classList.add('editor-preview-copy');
        copy.setAttribute('aria-hidden', 'true');
        copy.querySelectorAll('a').forEach(link => { link.tabIndex = -1; });
        copy.querySelectorAll('img').forEach(image => { image.addEventListener('error', () => image.remove()); });
        const reel = node('div', 'editor-preview-reel');
        reel.append(group, copy);
        previewTracks.replaceChildren(reel);
        previewPosition = 0;
        previewTracks.scrollLeft = 0;
        pauseUntil = performance.now() + 1200;
        requestAnimationFrame(measurePreview);
        const cards = edition.tracks.map(track => {
            const card = node('article', `editor-pick${track.highlight ? ' is-highlight' : ''}`);
            const body = node('div', 'editor-pick-body');
            if (track.highlight) body.append(node('p', 'editor-highlight', "★ This Year's Pick"));
            body.append(node('h3', '', track.title), node('p', 'editor-artist', track.artist));
            if ('reason' in track) body.append(node('p', `editor-reason${track.reason ? '' : ' is-missing'}`, track.reason || 'No individual selection reason available.'));
            const links = node('div', 'editor-track-links');
            for (const video of track.videos) {
                links.append(externalLink(`${video.label} ↗`, `https://www.youtube.com/watch?v=${video.id}`));
            }
            if (!track.videos.length) {
                links.append(externalLink('Search on YouTube ↗', `https://www.youtube.com/results?search_query=${encodeURIComponent(`${track.title} ${track.artist}`)}`));
            }
            body.append(links);
            card.append(cover(track), body);
            return card;
        });
        document.getElementById('editor-grid').replaceChildren(...cards);
        document.getElementById('editor-source-note')?.removeAttribute('hidden');
        message.hidden = true;
        preview.hidden = false;
    }

    function route(focus = false) {
        const showArchive = /^#editors-choice(?:-\d{4})?$/.test(location.hash);
        const requestedYear = Number(location.hash.match(/^#editors-choice-(\d{4})$/)?.[1]);
        if (editions.length && showArchive) {
            selectedYear = editions.some(item => item.year === requestedYear) ? requestedYear
                : (Number.isInteger(requestedYear) ? editions[0].year : (selectedYear || editions[0].year));
            history.replaceState(null, '', `#editors-choice-${selectedYear}`);
            renderEdition();
        }
        const switched = archive.hidden === showArchive;
        if (showArchive && switched) chartScroll = window.scrollY;
        charts.hidden = showArchive;
        archive.hidden = !showArchive;
        document.getElementById('charts-link').toggleAttribute('aria-current', !showArchive);
        document.getElementById('editors-link').toggleAttribute('aria-current', showArchive);
        document.getElementById(showArchive ? 'editors-link' : 'charts-link').setAttribute('aria-current', 'page');
        document.title = showArchive ? `${selectedYear || ''} Editor's Choice | Hype Wave`.trim() : 'Hype Wave | Music Chart';
        if (focus && switched) {
            if (showArchive) document.getElementById('editors-title').focus({preventScroll: true});
            else document.getElementById('charts-link').focus({preventScroll: true});
            window.scrollTo(0, showArchive ? 0 : chartScroll);
        }
        if (!showArchive && switched) requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
    }

    async function load() {
        retry.hidden = true;
        message.hidden = false;
        message.textContent = 'Loading selected tracks...';
        try {
            const response = await fetch('api/editors-choice.json');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            editions = validate(await response.json());
            for (const selector of [previewYear, archiveYear]) {
                selector.replaceChildren(...editions.map(edition => {
                    const option = node('option', '', String(edition.year));
                    option.value = edition.year;
                    return option;
                }));
            }
            selectedYear = editions[0].year;
            renderEdition();
            route();
        } catch (error) {
            console.error('Failed to load editorial collection:', error);
            message.textContent = 'Failed to load selected tracks. Please try again later.';
            retry.hidden = false;
        }
    }

    previewYear.addEventListener('change', () => {
        selectedYear = Number(previewYear.value);
        renderEdition();
    });
    archiveYear.addEventListener('change', () => { location.hash = `editors-choice-${archiveYear.value}`; });
    retry.addEventListener('click', load);
    window.addEventListener('hashchange', () => route(true));
    route();
    load();
})();
