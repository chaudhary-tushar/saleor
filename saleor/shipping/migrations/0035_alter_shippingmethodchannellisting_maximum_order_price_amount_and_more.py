# Generated by Django 4.2.15 on 2025-05-06 09:54

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shipping", "0034_shippingzone_countries_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shippingmethodchannellisting",
            name="maximum_order_price_amount",
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=20, null=True
            ),
        ),
        migrations.AlterField(
            model_name="shippingmethodchannellisting",
            name="minimum_order_price_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                default=Decimal("0.0"),
                max_digits=20,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="shippingmethodchannellisting",
            name="price_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal("0.0"), max_digits=20
            ),
        ),
    ]
