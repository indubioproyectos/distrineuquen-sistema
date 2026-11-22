// Large preview of a transfer receipt in «Cobrar» (19.0.1.129.1,
// views/cobrar.xml, the line form of distri.cobrar.wizard.line).
// An image shows as an image; a PDF is rendered inline with Odoo's PDF.js
// viewer, like the pdf_viewer widget. Unlike pdf_viewer it also previews a
// file that is not saved yet (the base64 value becomes an object URL), and
// it frees that URL when the file changes or the form closes.
import { Component, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { isBinarySize } from "@web/core/utils/binary";
import { url } from "@web/core/utils/urls";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

// base64 prefixes of the file signatures (models/comprobante_subir.py receipt_kind)
const MIME_BY_PREFIX = [
    ["JVBERi0", "application/pdf"],
    ["/9j/", "image/jpeg"],
    ["iVBORw0KGgo", "image/png"],
    ["UklGR", "image/webp"],
];
const PDF_VIEWER = "/web/static/lib/pdfjs/web/viewer.html";

export function receiptMime(base64, fileName) {
    if (base64 && !isBinarySize(base64)) {
        const hit = MIME_BY_PREFIX.find(([prefix]) => base64.startsWith(prefix));
        if (hit) {
            return hit[1];
        }
    }
    return /\.pdf$/i.test(fileName || "") ? "application/pdf" : "image/*";
}

export class ReceiptPreviewField extends Component {
    static template = "distri_pedido.ReceiptPreviewField";
    static props = {
        ...standardFieldProps,
        fileNameField: { type: String, optional: true },
    };

    setup() {
        this.objectUrl = null;
        this.objectFor = null;
        onWillUnmount(() => this.revoke());
    }

    revoke() {
        if (this.objectUrl) {
            URL.revokeObjectURL(this.objectUrl);
        }
        this.objectUrl = null;
        this.objectFor = null;
    }

    fileUrl(value, mime) {
        if (isBinarySize(value)) {
            // saved file: served by the record itself
            this.revoke();
            if (!this.props.record.resId) {
                return null;
            }
            return url("/web/content", {
                model: this.props.record.resModel,
                id: this.props.record.resId,
                field: this.props.name,
            });
        }
        if (this.objectFor !== value) {
            this.revoke();
            const binary = atob(value);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) {
                bytes[i] = binary.charCodeAt(i);
            }
            const type = mime === "image/*" ? "" : mime;
            this.objectUrl = URL.createObjectURL(new Blob([bytes], { type }));
            this.objectFor = value;
        }
        return this.objectUrl;
    }

    /** {url, pdf, open} of the file to show, or null when there is none. */
    getPreview() {
        const value = this.props.record.data[this.props.name];
        if (!value) {
            this.revoke();
            return null;
        }
        const fileName = this.props.fileNameField
            ? this.props.record.data[this.props.fileNameField]
            : "";
        const mime = receiptMime(value, fileName);
        let fileUrl;
        try {
            fileUrl = this.fileUrl(value, mime);
        } catch {
            return null; // not base64: nothing to show
        }
        if (!fileUrl) {
            return null;
        }
        const pdf = mime === "application/pdf";
        const viewer = pdf ? `${PDF_VIEWER}?file=${encodeURIComponent(fileUrl)}` : null;
        return { url: fileUrl, pdf, viewer, open: pdf ? viewer : fileUrl };
    }
}

export const receiptPreviewField = {
    component: ReceiptPreviewField,
    displayName: "Vista previa del comprobante",
    supportedTypes: ["binary"],
    extractProps: ({ attrs }) => ({ fileNameField: attrs.filename }),
};

registry.category("fields").add("distri_receipt_preview", receiptPreviewField);
