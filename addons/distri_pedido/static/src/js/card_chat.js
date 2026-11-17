// The receipts chat inside the confirmation card of the migration board
// (19.0.1.63.0, views/tarjeta_confirmacion.xml, models/tarjeta_confirmacion.py).
// The «Chat de comprobantes» viewer (chat_viewer.js) as a form widget: only the
// chat column, opened centered on the card's photo (or at the transfer's time
// when the card has none), loading more while scrolling. No search bar: the
// viewer in a new tab has it. It opens again on another card (pager) or when
// the card changes state (the bubble shows «Aprobada»).
import { onWillUpdateProps } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { ChatViewer } from "./chat_viewer";

export class CardChat extends ChatViewer {
    static template = "distri_pedido.CardChat";
    static props = { ...standardWidgetProps };
    static withPending = false;

    setup() {
        super.setup();
        this.state.denied = "";
        this.opened = this.cardKey(this.props);
        onWillUpdateProps((next) => {
            const key = this.cardKey(next);
            if (key !== this.opened) {
                this.opened = key;
                return this.open(this.initialAnchor(next));
            }
        });
    }

    cardKey(props) {
        const data = props.record.data;
        return `${props.record.resId}-${data.state}-${data.receipt_id?.id || 0}`;
    }

    initialAnchor(props = this.props) {
        return { message_id: null, collection_id: props.record.resId || null };
    }

    async open(anchor) {
        if (!anchor.collection_id) {
            this.state.entries = [];
            return;
        }
        try {
            await super.open(anchor);
            this.state.denied = "";
        } catch (error) {
            // the chat is for administración and gerencia only (visor_chat.py)
            this.state.denied = error?.data?.message || "No se pudo abrir el chat.";
            this.state.entries = [];
        }
    }
}

registry.category("view_widgets").add("distri_card_chat", {
    component: CardChat,
    additionalClasses: ["o_distri_card_chat_widget"],
});
