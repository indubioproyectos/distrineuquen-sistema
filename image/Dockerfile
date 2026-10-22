# Odoo 19 of the lab plus local OCR for payment receipts (distri_pedido,
# models/ocr_comprobante.py). Receipts are read on this server only: no
# cloud OCR. The base is pinned to the exact odoo:19 image the lab was
# running, so rebuilding gives the same Odoo.
FROM odoo@sha256:144175ec0039d52daff1d79f7e51c9281ca3c98b96c830feb49d09764a9f5d7c

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-spa python3-packaging \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir --no-deps --break-system-packages pytesseract==0.3.13
USER odoo
