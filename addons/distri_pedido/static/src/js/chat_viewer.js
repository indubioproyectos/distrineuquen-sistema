// Chat de comprobantes (19.0.1.46.0, models/visor_chat.py, views/visor_chat.xml).
// The transfers group as a chat: bubbles with sender, time, text and the
// photo or PDF, day separators, and the messages of one same minute grouped
// (WhatsApp exports print only the minute, so their order may vary). Opens
// centered on a receipt («Ver en el chat» on the card or the receipt) and
// loads more while scrolling up or down. The server decides which export is
// shown for each period and removes the duplicates; each bubble shows its
// source in small letters.
// 19.0.1.49.0: two columns, the chat on the left and every search result on
// the right (stacked and collapsible on a phone); clicking a result (or the
// arrow keys) centers the chat on it and keeps the list where it was. The day
// being read stays at the top while scrolling; each photo or PDF shows the
// date of the receipt itself.
import { Component, onMounted, onPatched, onWillStart, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

const MODEL = "distri.chat.viewer";
const EDGE_PX = 160;
const RESULTS_EDGE_PX = 200;
// sender names: the same name always gets the same colour; each one keeps
// a 4.5:1 contrast on the bubble background
const SENDER_COLORS = [
    "#1d5fb4", "#0a6b5c", "#8f2d1c", "#6b3fa0", "#9a5b00",
    "#1f6b45", "#b0306a", "#3c4a57", "#00708a", "#7a4b00",
];
const pesos = new Intl.NumberFormat("es-AR", { maximumFractionDigits: 2 });

export function senderColor(name) {
    let hash = 0;
    for (const char of name || "") {
        hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    }
    return SENDER_COLORS[hash % SENDER_COLORS.length];
}

// [{type: "day", label} | {type: "minute", time, entries, multi}]: messages of
// the same minute together, one separator per Argentine day
export function buildRows(entries) {
    const rows = [];
    let day = null;
    let minute = null;
    for (const entry of entries) {
        if (entry.day !== day) {
            day = entry.day;
            minute = null;
            rows.push({ type: "day", key: `day-${entry.day}`, label: entry.day_label });
        }
        if (!minute || minute.minute !== entry.minute) {
            minute = { type: "minute", key: `min-${entry.minute}-${entry.key}`,
                       minute: entry.minute, time: entry.time, entries: [] };
            rows.push(minute);
        }
        minute.entries.push(entry);
    }
    for (const row of rows) {
        if (row.type === "minute") {
            row.multi = row.entries.length > 1;
            row.unknownPos = row.multi && row.entries.some((e) => !e.pos_known);
        }
    }
    return rows;
}

// the same letter without accents nor case, for the highlight
function fold(char) {
    return char.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

// [{text, match}]: `text` cut where `query` appears (any case or accent)
export function highlightParts(text, query) {
    text = text || "";
    const needle = [...(query || "").trim()].map(fold).join("");
    if (!needle) {
        return [{ text, match: false }];
    }
    const chars = [...text];
    const folded = chars.map(fold);
    // start position (in chars) of each folded char of the text
    const starts = [];
    let haystack = "";
    folded.forEach((piece, index) => {
        for (let i = 0; i < piece.length; i++) {
            starts.push(index);
        }
        haystack += piece;
    });
    const parts = [];
    let from = 0;
    let at = haystack.indexOf(needle);
    while (at !== -1) {
        const begin = starts[at];
        const end = starts[at + needle.length - 1] + 1;
        if (begin > from) {
            parts.push({ text: chars.slice(from, begin).join(""), match: false });
        }
        parts.push({ text: chars.slice(begin, end).join(""), match: true });
        from = end;
        at = haystack.indexOf(needle, at + needle.length);
    }
    if (from < chars.length) {
        parts.push({ text: chars.slice(from).join(""), match: false });
    }
    return parts;
}

export class ChatViewer extends Component {
    static template = "distri_pedido.ChatViewer";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.scrollRef = useRef("scroll");
        this.resultsRef = useRef("results");
        this.state = useState({
            entries: [], anchor: null, note: "", first: false, last: false,
            moreBefore: false, moreAfter: false, loading: false, ownSince: "",
            query: "", hits: null, searching: false, preview: null,
            // the search shown in the right column
            searched: "", total: 0, moreHits: false, selected: null, resultsOpen: true,
            currentDay: "",
        });
        this.pendingScroll = null;
        this.pendingFocus = null;
        onWillStart(() => this.open(this.initialAnchor()));
        // 19.0.1.52.0: the photos above the anchor load after the first
        // scroll and push it down; while the person has not moved the chat
        // (a few seconds at most), every photo that loads centers it again
        this.holdUntil = 0;
        onMounted(() => {
            const el = this.scrollRef.el;
            el?.addEventListener("load", (ev) => {
                if (ev.target.tagName === "IMG" && this.holdUntil > Date.now()) {
                    this.centerAnchor();
                }
            }, true);
            for (const type of ["wheel", "touchstart", "pointerdown", "keydown"]) {
                el?.addEventListener(type, () => { this.holdUntil = 0; }, { passive: true });
            }
            this.applyScroll();
        });
        onPatched(() => {
            this.applyScroll();
            if (this.pendingFocus) {
                this.resultsRef.el?.querySelector(
                    `[data-hit="${CSS.escape(this.pendingFocus)}"]`)?.focus({ preventScroll: true });
                this.pendingFocus = null;
            }
            // Esc closes the enlarged photo
            this.scrollRef.el?.parentElement.querySelector(".o_distri_chat_preview")?.focus();
        });
    }

    // what the chat opens on: the action's message or card (19.0.1.63.0: the
    // card's own chat, static/src/js/card_chat.js, opens on its record)
    initialAnchor() {
        const params = this.props.action?.params || {};
        return {
            message_id: params.message_id || null,
            collection_id: params.collection_id || null,
        };
    }

    get rows() {
        return buildRows(this.state.entries);
    }

    async open(anchor) {
        this.state.loading = true;
        try {
            const data = await this.orm.call(MODEL, "distri_chat_open", [], anchor);
            Object.assign(this.state, {
                entries: data.entries, anchor: data.anchor, note: data.note,
                first: data.first, last: data.last, moreBefore: data.more_before,
                moreAfter: data.more_after, ownSince: data.own_since,
            });
            this.pendingScroll = { type: data.anchor ? "anchor" : (anchor.at ? "middle" : "bottom") };
        } finally {
            this.state.loading = false;
        }
    }

    async loadMore(direction) {
        if (this.state.loading) {
            return;
        }
        const before = direction === "before";
        if (before ? !this.state.moreBefore : !this.state.moreAfter) {
            return;
        }
        this.state.loading = true;
        const el = this.scrollRef.el;
        try {
            const data = await this.orm.call(MODEL, "distri_chat_more", [
                direction, before ? this.state.first : this.state.last,
            ]);
            if (before) {
                // keep what the person is looking at in place
                this.pendingScroll = { type: "keep", height: el.scrollHeight, top: el.scrollTop };
                this.state.entries = [...data.entries, ...this.state.entries];
                this.state.first = data.cursor;
                this.state.moreBefore = data.more;
            } else {
                this.state.entries = [...this.state.entries, ...data.entries];
                this.state.last = data.cursor;
                this.state.moreAfter = data.more;
            }
        } finally {
            this.state.loading = false;
        }
    }

    applyScroll() {
        const el = this.scrollRef.el;
        const pending = this.pendingScroll;
        if (!el || !pending) {
            return;
        }
        this.pendingScroll = null;
        if (pending.type === "keep") {
            el.scrollTop = pending.top + (el.scrollHeight - pending.height);
        } else if (pending.type === "anchor") {
            this.centerAnchor();
            this.holdUntil = Date.now() + 8000;
        } else if (pending.type === "middle") {
            el.scrollTop = (el.scrollHeight - el.clientHeight) / 2;
        } else {
            el.scrollTop = el.scrollHeight;
        }
        this.updateDay();
    }

    // the anchor in the middle of the chat column, measured on screen (its
    // offsetTop is relative to its minute group, not to the scroll box)
    centerAnchor() {
        const el = this.scrollRef.el;
        const target = el?.querySelector(".o_distri_chat_anchor");
        if (!target) {
            return;
        }
        const offset = target.getBoundingClientRect().top - el.getBoundingClientRect().top;
        el.scrollTop += offset - (el.clientHeight - target.offsetHeight) / 2;
    }

    // the day of the topmost message in view, for the header that stays
    updateDay() {
        const el = this.scrollRef.el;
        if (!el) {
            return;
        }
        let label = "";
        for (const sep of el.querySelectorAll(".o_distri_chat_day")) {
            if (sep.offsetTop > el.scrollTop + 4) {
                break;
            }
            label = sep.dataset.label;
        }
        if (label !== this.state.currentDay) {
            this.state.currentDay = label;
        }
    }

    onScroll() {
        const el = this.scrollRef.el;
        if (!el) {
            return;
        }
        this.updateDay();
        if (this.state.loading) {
            return;
        }
        if (el.scrollTop < EDGE_PX) {
            this.loadMore("before");
        } else if (el.scrollHeight - el.scrollTop - el.clientHeight < EDGE_PX) {
            this.loadMore("after");
        }
    }

    async onSearch(ev) {
        ev.preventDefault();
        const query = this.state.query.trim();
        if (!query) {
            this.closeHits();
            return;
        }
        this.state.searching = true;
        try {
            const data = await this.orm.call(MODEL, "distri_chat_search", [query]);
            if (data.jump) {
                await this.open({ at: data.jump });
            } else {
                Object.assign(this.state, {
                    hits: data.hits, total: data.total, moreHits: data.more,
                    searched: query, selected: null, resultsOpen: true,
                });
                if (this.resultsRef.el) {
                    this.resultsRef.el.scrollTop = 0;
                }
            }
        } finally {
            this.state.searching = false;
        }
    }

    async loadMoreHits() {
        if (this.state.searching || !this.state.moreHits) {
            return;
        }
        this.state.searching = true;
        try {
            const data = await this.orm.call(MODEL, "distri_chat_search", [this.state.searched], {
                offset: this.state.hits.length,
            });
            const seen = new Set(this.state.hits.map((h) => h.key));
            this.state.hits = [...this.state.hits, ...data.hits.filter((h) => !seen.has(h.key))];
            this.state.total = data.total;
            this.state.moreHits = data.more;
        } finally {
            this.state.searching = false;
        }
    }

    onResultsScroll() {
        const el = this.resultsRef.el;
        if (el && el.scrollHeight - el.scrollTop - el.clientHeight < RESULTS_EDGE_PX) {
            this.loadMoreHits();
        }
    }

    async goTo(hit) {
        this.state.selected = hit.key;
        if (this.state.entries.some((e) => e.key === hit.key)) {
            // already loaded: only center it
            const same = this.state.anchor === hit.key;
            this.state.anchor = hit.key;
            this.state.note = "";
            this.pendingScroll = { type: "anchor" };
            if (same) {
                this.applyScroll();  // nothing to render: scroll now
            }
        } else if (hit.message_id) {
            await this.open({ message_id: hit.message_id });
        } else {
            await this.open({ at: hit.ts });
            this.state.anchor = hit.key;
            this.pendingScroll = { type: "anchor" };
        }
    }

    async onResultsKey(ev) {
        if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") {
            return;
        }
        const hits = this.state.hits || [];
        if (!hits.length) {
            return;
        }
        ev.preventDefault();
        const index = hits.findIndex((h) => h.key === this.state.selected);
        let next = ev.key === "ArrowDown" ? index + 1 : index - 1;
        if (index === -1) {
            next = 0;
        }
        if (next >= hits.length && this.state.moreHits) {
            await this.loadMoreHits();
        }
        const hit = this.state.hits[Math.max(0, Math.min(next, this.state.hits.length - 1))];
        this.pendingFocus = hit.key;
        await this.goTo(hit);
    }

    toggleResults() {
        this.state.resultsOpen = !this.state.resultsOpen;
    }

    closeHits() {
        Object.assign(this.state, {
            hits: null, total: 0, moreHits: false, searched: "", selected: null,
        });
    }

    parts(text) {
        return highlightParts(text, this.state.searched);
    }

    hitThumbUrl(hit) {
        return `/web/image/${hit.attachment_id}/96x96`;
    }

    senderStyle(entry) {
        return `color: ${senderColor(entry.sender)}`;
    }

    bubbleStyle(entry) {
        return `border-left-color: ${senderColor(entry.sender)}`;
    }

    thumbUrl(entry) {
        return `/web/image/${entry.attachment_id}/480x480`;
    }

    fileUrl(entry) {
        return `/web/content/${entry.attachment_id}`;
    }

    money(value) {
        return `$ ${pesos.format(value || 0)}`;
    }

    openPreview(entry) {
        this.state.preview = entry;
    }

    closePreview() {
        this.state.preview = null;
    }

    onPreviewLink(ev) {
        ev.stopPropagation();  // open the file without closing the preview
    }

    onPreviewKey(ev) {
        if (ev.key === "Escape") {
            this.closePreview();
        }
    }

    async createCard(entry) {
        const cardId = await this.orm.call("distri.chat.viewer", "distri_chat_create_card",
                                           [entry.message_id]);
        if (!cardId) {
            this.notification.add("Es una copia de un comprobante ya recibido: no lleva tarjeta propia.",
                                  { type: "warning" });
            return;
        }
        entry.collection_id = cardId;
        entry.card_state = "draft";
        await this.openCard(entry);
    }

    async openCard(entry) {
        await this.action.doAction({
            type: "ir.actions.act_window", res_model: "distri.mp.collection",
            res_id: entry.collection_id, views: [[false, "form"]], target: "new",
        });
    }
}

registry.category("actions").add("distri_chat_viewer", ChatViewer);
