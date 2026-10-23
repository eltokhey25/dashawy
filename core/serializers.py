"""Serializers for the customer and technician request APIs.

The wire shapes follow ``backend/routes/customer.routes.js`` and
``backend/routes/technician.routes.js``: request rows expose ``customer_id`` /
``technician_id`` and the joined display fields the browser already reads.
"""

from rest_framework import serializers

from .models import Request, Specialty
from .upload_validators import validate_document_upload
from .validation import STATIC_SPECIALTY_KEYS, resolve_location


class OptionalFloatField(serializers.FloatField):
    """A float where the legacy client's empty string means "not supplied"."""

    def to_internal_value(self, data):
        if data in ('', None):
            return None
        return super().to_internal_value(data)


def enabled_specialty_keys():
    """Keys customers may request, falling back to the static config.

    The database is authoritative once seeded, so an admin disabling a
    specialty removes it from the public form. The static list only keeps a
    fresh, unseeded database usable (and mirrors the Node config).
    """
    keys = list(
        Specialty.objects.filter(enabled=True).values_list('key', flat=True)
    )
    return keys or list(STATIC_SPECIALTY_KEYS)


class CreateRequestSerializer(serializers.Serializer):
    """Validate the body of ``POST /api/customer/requests``."""

    specialty = serializers.CharField(
        max_length=100,
        error_messages={
            'required': 'نوع الخدمة مطلوب',
            'blank': 'نوع الخدمة مطلوب',
        },
    )
    custom_specialty_text = serializers.CharField(
        required=False, allow_blank=True, max_length=200
    )
    description = serializers.CharField(
        max_length=2000,
        error_messages={
            'required': 'يرجى وصف المشكلة (حرف واحد على الأقل)',
            'blank': 'يرجى وصف المشكلة (حرف واحد على الأقل)',
            'max_length': 'وصف المشكلة طويل جدًا (2000 حرف كحد أقصى)',
        },
    )
    location_text = serializers.CharField(
        required=False, allow_blank=True, max_length=300,
        error_messages={'max_length': 'وصف الموقع طويل جدًا (300 حرف كحد أقصى)'},
    )
    area_key = serializers.CharField(required=False, allow_blank=True, max_length=64)
    latitude = OptionalFloatField(required=False, allow_null=True)
    longitude = OptionalFloatField(required=False, allow_null=True)

    def validate_specialty(self, value):
        key = value.strip()
        if key not in enabled_specialty_keys():
            raise serializers.ValidationError('نوع الخدمة غير صالح')
        return key

    def validate(self, attrs):
        specialty = attrs['specialty']

        custom_text = (attrs.get('custom_specialty_text') or '').strip()
        if specialty == 'other' and not custom_text:
            raise serializers.ValidationError('يرجى كتابة اسم الخدمة المطلوبة')

        latitude = attrs.get('latitude')
        longitude = attrs.get('longitude')
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError('إحداثيات الموقع غير صالحة')
        if latitude is not None:
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise serializers.ValidationError('إحداثيات الموقع غير صالحة')

        try:
            resolved_location = resolve_location(
                attrs.get('area_key'), attrs.get('location_text')
            )
        except KeyError:
            raise serializers.ValidationError('المنطقة غير صالحة')

        attrs['custom_specialty_text'] = custom_text if specialty == 'other' else None
        attrs['resolved_location'] = resolved_location
        return attrs


class RequestSerializer(serializers.ModelSerializer):
    """A request row plus the joined customer/technician display fields."""

    customer_id = serializers.IntegerField(read_only=True)
    technician_id = serializers.IntegerField(read_only=True, allow_null=True)

    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(
        source='customer.phone', read_only=True, allow_null=True
    )
    customer_location = serializers.CharField(
        source='customer.location_text', read_only=True, allow_null=True
    )
    customer_latitude = serializers.FloatField(
        source='customer.latitude', read_only=True, allow_null=True
    )
    customer_longitude = serializers.FloatField(
        source='customer.longitude', read_only=True, allow_null=True
    )

    technician_name = serializers.CharField(
        source='technician.name', read_only=True, allow_null=True
    )
    technician_phone = serializers.CharField(
        source='technician.phone', read_only=True, allow_null=True
    )
    technician_specialty = serializers.CharField(
        source='technician.specialty', read_only=True, allow_null=True
    )
    technician_location = serializers.CharField(
        source='technician.location_text', read_only=True, allow_null=True
    )
    technician_profile_url = serializers.SerializerMethodField()

    class Meta:
        model = Request
        # ``customer`` / ``technician`` would serialise the bare FK value under
        # the wrong key; the legacy rows expose ``*_id`` instead.
        exclude = ('customer', 'technician')

    def get_technician_profile_url(self, obj):
        if not obj.technician_id:
            return None
        return '/technician-profile.html?id=' + str(obj.technician_id)


class TechnicianRequestSerializer(RequestSerializer):
    """Technician-facing request: never includes the doorstep start PIN."""

    class Meta(RequestSerializer.Meta):
        exclude = tuple(RequestSerializer.Meta.exclude) + ('start_pin',)


class TechnicianDocumentUploadSerializer(serializers.Serializer):
    """Validate the two private documents submitted during onboarding.

    The actual view must save the files below ``MEDIA_ROOT/documents/`` and must
    stream them back through an authenticated endpoint, never through a public
    media URL.
    """

    criminal_record = serializers.FileField(
        validators=[validate_document_upload],
        write_only=True,
    )
    national_id = serializers.FileField(
        validators=[validate_document_upload],
        write_only=True,
    )
