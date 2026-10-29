// «Deudores» (views/deudores.xml, js_class="distri_debtor_form").
// The standard form, except that changing a filter («Vendedor», «Fecha de
// corte», «Deuda mínima») saves the record at once. The onchange already
// shows the new selection; the save brings the list back in debt order (an
// onchange appends the lines it adds at the end), rebuilds the snapshot for a
// new cut date and leaves on the server exactly what the PDF, the Excel and
// the presets will read (models/deudores.py, _compute_selection).
import { registry } from "@web/core/registry";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";

const FILTERS = ["seller", "date_to", "min_amount"];

export class DebtorReportFormController extends FormController {
    get modelParams() {
        const params = super.modelParams;
        params.hooks.onRecordChanged = (record, changes) => {
            if (record === this.model.root && FILTERS.some((name) => name in changes)) {
                // not awaited: the hook runs inside the model's mutex and the
                // save queues right after the change
                record.save();
            }
        };
        return params;
    }
}

registry.category("views").add("distri_debtor_form", {
    ...formView,
    Controller: DebtorReportFormController,
});
