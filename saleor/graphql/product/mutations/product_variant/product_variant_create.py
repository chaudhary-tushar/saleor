import graphene
from django.core.exceptions import ValidationError

from .....attribute import models as attribute_models
from .....core.tracing import traced_atomic_transaction
from .....discount.utils.promotion import mark_active_catalogue_promotion_rules_as_dirty
from .....permission.enums import ProductPermissions
from .....product import models
from .....product.error_codes import ProductErrorCode
from .....product.utils.variants import generate_and_set_variant_name
from ....attribute.types import AttributeValueInput
from ....attribute.utils.attribute_assignment import (
    AttributeAssignmentMixin,
)
from ....attribute.utils.shared import AttrValuesInput
from ....core import ResolveInfo
from ....core.context import ChannelContext
from ....core.doc_category import DOC_CATEGORY_PRODUCTS
from ....core.mutations import DeprecatedModelMutation
from ....core.scalars import DateTime, WeightScalar
from ....core.types import BaseInputObjectType, NonNullList, ProductError
from ....core.utils import get_duplicated_values
from ....meta.inputs import MetadataInput, MetadataInputDescription
from ....plugins.dataloaders import get_plugin_manager_promise
from ....shop.utils import get_track_inventory_by_default
from ....warehouse.types import Warehouse
from ...types import ProductVariant
from ...utils import (
    clean_variant_sku,
    create_stocks,
    get_used_variants_attribute_values,
)
from ..product.product_create import StockInput
from . import product_variant_cleaner as cleaner

T_INPUT_MAP = list[tuple[attribute_models.Attribute, AttrValuesInput]]


class PreorderSettingsInput(BaseInputObjectType):
    global_threshold = graphene.Int(
        description="The global threshold for preorder variant."
    )
    end_date = DateTime(description="The end date for preorder.")

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class ProductVariantInput(BaseInputObjectType):
    attributes = NonNullList(
        AttributeValueInput,
        required=False,
        description="List of attributes specific to this variant.",
    )
    sku = graphene.String(description="Stock keeping unit.")
    name = graphene.String(description="Variant name.", required=False)
    track_inventory = graphene.Boolean(
        description=(
            "Determines if the inventory of this variant should be tracked. If false, "
            "the quantity won't change when customers buy this item. "
            "If the field is not provided, `Shop.trackInventoryByDefault` will be used."
        )
    )
    weight = WeightScalar(description="Weight of the Product Variant.", required=False)
    preorder = PreorderSettingsInput(
        description=("Determines if variant is in preorder.")
    )
    quantity_limit_per_customer = graphene.Int(
        required=False,
        description=(
            "Determines maximum quantity of `ProductVariant`,"
            "that can be bought in a single checkout."
        ),
    )
    metadata = NonNullList(
        MetadataInput,
        description="Fields required to update the product variant metadata. "
        f"{MetadataInputDescription.PUBLIC_METADATA_INPUT}",
        required=False,
    )
    private_metadata = NonNullList(
        MetadataInput,
        description="Fields required to update the product variant private metadata. "
        f"{MetadataInputDescription.PRIVATE_METADATA_INPUT}",
        required=False,
    )
    external_reference = graphene.String(
        description="External ID of this product variant.",
        required=False,
    )

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class ProductVariantCreateInput(ProductVariantInput):
    attributes = NonNullList(
        AttributeValueInput,
        required=True,
        description="List of attributes specific to this variant.",
    )
    product = graphene.ID(
        description="Product ID of which type is the variant.",
        name="product",
        required=True,
    )
    stocks = NonNullList(
        StockInput,
        description="Stocks of a product available for sale.",
        required=False,
    )

    class Meta:
        doc_category = DOC_CATEGORY_PRODUCTS


class ProductVariantCreate(DeprecatedModelMutation):
    class Arguments:
        input = ProductVariantCreateInput(
            required=True, description="Fields required to create a product variant."
        )

    class Meta:
        description = "Creates a new variant for a product."
        model = models.ProductVariant
        object_type = ProductVariant
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"
        errors_mapping = {"price_amount": "price"}
        support_meta_field = True
        support_private_meta_field = True

    @classmethod
    def clean_input(
        cls,
        info: ResolveInfo,
        instance: models.ProductVariant,
        data: dict,
        **kwargs,
    ):
        cleaned_input = super().clean_input(info, instance, data, **kwargs)

        cleaner.clean_weight(cleaned_input)
        cleaner.clean_quantity_limit(cleaned_input)
        if stocks := cleaned_input.get("stocks"):
            cls.check_for_duplicates_in_stocks(stocks)
        cls.clean_attributes(cleaned_input, instance)
        if "sku" in cleaned_input:
            cleaned_input["sku"] = clean_variant_sku(cleaned_input.get("sku"))
        cleaner.clean_preorder_settings(cleaned_input)

        return cleaned_input

    @classmethod
    def clean_attributes(cls, cleaned_input: dict, instance: models.ProductVariant):
        product = cls.get_product(cleaned_input)
        product_type = product.product_type
        used_attribute_values = get_used_variants_attribute_values(product)

        variant_attributes_ids = {
            graphene.Node.to_global_id("Attribute", attr_id)
            for attr_id in list(
                product_type.variant_attributes.all().values_list("pk", flat=True)
            )
        }

        attributes = cleaned_input.get("attributes")
        attributes_ids = {attr["id"] for attr in attributes or []}
        invalid_attributes = attributes_ids - variant_attributes_ids
        if len(invalid_attributes) > 0:
            raise ValidationError(
                "Given attributes are not a variant attributes.",
                code=ProductErrorCode.ATTRIBUTE_CANNOT_BE_ASSIGNED.value,
                params={"attributes": invalid_attributes},
            )

        # Run the validation only if product type is configurable
        if product_type.has_variants:
            # Attributes are provided as list of `AttributeValueInput` objects.
            # We need to transform them into the format they're stored in the
            # `Product` model, which is HStore field that maps attribute's PK to
            # the value's PK.
            try:
                if attributes:
                    attributes_qs = product_type.variant_attributes.all()
                    cleaned_attributes: T_INPUT_MAP = (
                        AttributeAssignmentMixin.clean_input(attributes, attributes_qs)
                    )
                    cleaner.validate_duplicated_attribute_values(
                        cleaned_attributes, used_attribute_values
                    )
                    cleaned_input["attributes"] = cleaned_attributes
                elif product_type.variant_attributes.filter(value_required=True):
                    # if attributes were not provided on creation
                    raise ValidationError(
                        "All required attributes must take a value.",
                        ProductErrorCode.REQUIRED.value,
                    )
            except ValidationError as e:
                raise ValidationError({"attributes": e}) from e
        else:
            if attributes:
                raise ValidationError(
                    "Cannot assign attributes for product type without variants",
                    ProductErrorCode.INVALID.value,
                )

    @classmethod
    def get_product(cls, cleaned_input: dict) -> models.Product:
        product = cleaned_input["product"]
        if not product:
            raise ValidationError(
                {
                    "product": ValidationError(
                        "Product cannot be set empty.",
                        code=ProductErrorCode.INVALID.value,
                    )
                }
            )
        return product

    @classmethod
    def check_for_duplicates_in_stocks(cls, stocks_data):
        warehouse_ids = [stock["warehouse"] for stock in stocks_data]
        duplicates = get_duplicated_values(warehouse_ids)
        if duplicates:
            error_msg = "Duplicated warehouse ID: {}".format(", ".join(duplicates))
            raise ValidationError(
                {
                    "stocks": ValidationError(
                        error_msg, code=ProductErrorCode.UNIQUE.value
                    )
                }
            )

    @classmethod
    def set_track_inventory(cls, _info, instance, cleaned_input):
        track_inventory_by_default = get_track_inventory_by_default(_info)
        track_inventory = cleaned_input.get("track_inventory")
        if track_inventory_by_default is not None:
            instance.track_inventory = (
                track_inventory_by_default
                if track_inventory is None
                else track_inventory
            )

    @classmethod
    def save(cls, info: ResolveInfo, instance, cleaned_input):
        new_variant = instance.pk is None
        cls.set_track_inventory(info, instance, cleaned_input)
        with traced_atomic_transaction():
            instance.save()
            if not instance.product.default_variant:
                instance.product.default_variant = instance
                instance.product.save(update_fields=["default_variant", "updated_at"])
            stocks = cleaned_input.get("stocks")
            if stocks:
                cls.create_variant_stocks(instance, stocks)
            attributes = cleaned_input.get("attributes")
            if attributes:
                AttributeAssignmentMixin.save(instance, attributes)

            if not instance.name:
                generate_and_set_variant_name(instance, cleaned_input.get("sku"))

            manager = get_plugin_manager_promise(info.context).get()
            instance.product.search_index_dirty = True
            instance.product.save(update_fields=["search_index_dirty"])
            event_to_call = (
                manager.product_variant_created
                if new_variant
                else manager.product_variant_updated
            )
            cls.call_event(event_to_call, instance)

    @classmethod
    def post_save_action(cls, info: ResolveInfo, instance, cleaned_input):
        channel_ids = models.ProductChannelListing.objects.filter(
            product_id=instance.product_id
        ).values_list("channel_id", flat=True)
        # This will recalculate discounted prices for products.
        cls.call_event(mark_active_catalogue_promotion_rules_as_dirty, channel_ids)

    @classmethod
    def create_variant_stocks(cls, variant, stocks):
        warehouse_ids = [stock["warehouse"] for stock in stocks]
        warehouses = cls.get_nodes_or_error(
            warehouse_ids, "warehouse", only_type=Warehouse
        )
        create_stocks(variant, stocks, warehouses)

    @classmethod
    def success_response(cls, instance):
        instance = ChannelContext(node=instance, channel_slug=None)
        return super().success_response(instance)
