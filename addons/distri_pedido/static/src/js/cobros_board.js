// Tablero de cobros (views/cobros.xml, js_class="distri_cobros_kanban").
// The standard kanban with a richer column header: name, card count, total
// amount in pesos, a colour accent per column and an «i» that explains what
// to do in that column; the columns that need a person («Asignar cliente»,
// «Sin transferencia») stand out. The texts come from the server (COLUMNS and
// COLUMN_HELP in models/cobros.py) through `distri_board_columns`.
import { Component, onWillStart, onWillUpdateProps, useState, useSubEnv } from "@odoo/owl";
import { usePopover } from "@web/core/popover/popover_hook";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { KanbanHeader } from "@web/views/kanban/kanban_header";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { kanbanView } from "@web/views/kanban/kanban_view";

const BOARD_MODEL = "distri.mp.collection";

// Icon next to the column name: the colour is never the only signal.
const COLUMN_ICONS = {
    verify: "fa-search",
    confirm: "fa-check-circle",
    review: "fa-eye",
    unidentified: "fa-user-plus",
    no_money: "fa-exclamation-triangle",
};

const pesos = new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 });

export function formatPesos(value) {
    return `$ ${pesos.format(Math.round(value || 0))}`;
}

export class ColumnHelpPopover extends Component {
    static template = "distri_pedido.ColumnHelpPopover";
    static props = { title: String, text: String, close: Function };
}

export class CobrosKanbanHeader extends KanbanHeader {
    static template = "distri_pedido.CobrosKanbanHeader";

    setup() {
        super.setup();
        this.helpState = useState({ open: false });
        this.helpPopover = usePopover(ColumnHelpPopover, {
            position: "bottom",
            popoverClass: "o_distri_col_help_popover",
            onClose: () => (this.helpState.open = false),
        });
    }

    get columnKey() {
        return this.group.value || "";
    }

    get columnInfo() {
        return this.env.distriBoardColumns?.[this.columnKey];
    }

    get columnHelp() {
        return this.columnInfo?.help || "";
    }

    get isPriority() {
        return Boolean(this.columnInfo?.priority);
    }

    get columnIcon() {
        return COLUMN_ICONS[this.columnKey] || "";
    }

    get columnTotal() {
        return formatPesos(this.group.aggregates?.amount);
    }

    onHelpClick(ev) {
        if (this.helpPopover.isOpen) {
            this.helpPopover.close();
            return;
        }
        this.helpState.open = true;
        this.helpPopover.open(ev.currentTarget, {
            title: this.columnInfo?.label || this.group.displayName,
            text: this.columnHelp,
        });
    }
}

export class CobrosKanbanRenderer extends KanbanRenderer {
    static components = { ...KanbanRenderer.components, KanbanHeader: CobrosKanbanHeader };

    setup() {
        super.setup();
        const orm = useService("orm");
        const columns = {};
        useSubEnv({ distriBoardColumns: columns });
        onWillStart(async () => {
            const list = await orm.call(BOARD_MODEL, "distri_board_columns", []);
            for (const column of list) {
                columns[column.key] = column;
            }
        });
    }

    getGroupClasses(group, isGroupProcessing) {
        const classes = super.getGroupClasses(group, isGroupProcessing);
        if (!group.value) {
            return classes;
        }
        const priority = this.env.distriBoardColumns[group.value]?.priority;
        return `${classes} o_distri_col o_distri_col_${group.value}${
            priority ? " o_distri_col_priority" : " o_distri_col_calm"
        }`;
    }
}

// Bar on top of the board (19.0.1.31.0, models/mayoristas.py): what to act on
// for retail, the unknown payers grouped by payer and the set-aside
// mayoristas, all counted in SQL by `distri_board_counters`.
export class CobrosCounters extends Component {
    static template = "distri_pedido.CobrosCounters";
    static props = { list: Object };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ data: null });
        onWillStart(() => this.load());
        onWillUpdateProps(() => this.load());
    }

    async load() {
        try {
            this.state.data = await this.orm.call(BOARD_MODEL, "distri_board_counters", []);
        } catch {
            this.state.data = null;  // the board works without the bar
        }
    }

    money(value) {
        return formatPesos(value);
    }

    async open(method) {
        const action = await this.orm.call(BOARD_MODEL, method, []);
        await this.action.doAction(action);
    }
}

export class CobrosKanbanController extends KanbanController {
    static template = "distri_pedido.CobrosKanbanView";
    static components = { ...KanbanController.components, CobrosCounters };

    // the header shows the total of each column: read the sum of `amount`
    // with the groups
    get modelParams() {
        const params = super.modelParams;
        const toAggregate = params.config.fieldsToAggregate || [];
        if (!toAggregate.includes("amount")) {
            params.config.fieldsToAggregate = [...toAggregate, "amount"];
        }
        return params;
    }
}

export const cobrosKanbanView = {
    ...kanbanView,
    Controller: CobrosKanbanController,
    Renderer: CobrosKanbanRenderer,
};

registry.category("views").add("distri_cobros_kanban", cobrosKanbanView);
