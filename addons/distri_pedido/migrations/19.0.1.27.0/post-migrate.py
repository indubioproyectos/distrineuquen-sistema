"""«Pedido sin cobrar» keeps its original amount (19.0.1.27.0): the items of
19.0.1.26.0 start from the amount they had."""


def migrate(cr, version):
    cr.execute("""
        UPDATE distri_promised_transfer SET original_amount = amount
         WHERE kind = 'unpaid' AND original_amount IS NULL""")
