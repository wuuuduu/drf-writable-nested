# -*- coding: utf-8 -*-
from collections import OrderedDict, defaultdict

from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db.models import ProtectedError, FieldDoesNotExist
from django.db.models.fields.related import ForeignObjectRel
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.validators import UniqueValidator


class BaseNestedModelSerializer(serializers.ModelSerializer):
    def _extract_relations(self, validated_data):
        reverse_relations = OrderedDict()
        relations = OrderedDict()

        # Remove related fields from validated data for future manipulations
        for field_name, field in self.fields.items():
            if field.read_only:
                continue
            try:
                related_field, direct = self._get_related_field(field)
            except FieldDoesNotExist:
                continue

            if isinstance(field, serializers.ListSerializer) and \
                    isinstance(field.child, serializers.ModelSerializer):
                if field.source not in validated_data:
                    # Skip field if field is not required
                    continue

                validated_data.pop(field.source)

                reverse_relations[field_name] = (
                    related_field, field.child, field.source)

            if isinstance(field, serializers.ModelSerializer):
                if field.source not in validated_data:
                    # Skip field if field is not required
                    continue

                if validated_data.get(field.source) is None:
                    if direct:
                        # Don't process null value for direct relations
                        # Native create/update processes these values
                        continue

                validated_data.pop(field.source)
                # Reversed one-to-one looks like direct foreign keys but they
                # are reverse relations
                if direct:
                    relations[field_name] = (
                        related_field, field, field.source)
                else:
                    reverse_relations[field_name] = (
                        related_field, field, field.source)

        return relations, reverse_relations

    def _get_related_field(self, field):
        model_class = self.Meta.model

        try:
            related_field = model_class._meta.get_field(field.source)
        except FieldDoesNotExist:
            # If `related_name` is not set, field name does not include
            # `_set` -> remove it and check again
            default_postfix = '_set'
            if field.source.endswith(default_postfix):
                related_field = model_class._meta.get_field(
                    field.source[:-len(default_postfix)])
            else:
                raise

        if isinstance(related_field, ForeignObjectRel):
            return related_field.field, False
        return related_field, True

    def _get_serializer_for_field(self, field, **kwargs):
        kwargs.update({
            'context': self.context,
            'partial': self.partial if kwargs.get('instance') else False,
        })

        # if field is a polymorphic serializer
        if hasattr(field, '_get_serializer_from_resource_type'):
            # get 'real' serializer based on resource type
            serializer = field._get_serializer_from_resource_type(
                kwargs.get('data').get(field.resource_type_field_name)
            )

            return serializer.__class__(**kwargs)
        else:
            return field.__class__(**kwargs)

    def _get_generic_lookup(self, instance, related_field):
        return {
            related_field.content_type_field_name:
                ContentType.objects.get_for_model(instance),
            related_field.object_id_field_name: instance.pk,
        }

    def _get_related_pk(self, data, model_class):
        pk = data.get('pk') or data.get(model_class._meta.pk.attname)

        if pk:
            return str(pk)

        return None

    def _extract_related_pks(self, field, related_data):
        model_class = field.Meta.model
        return {self._get_related_pk(d, model_class) for d in related_data if d}

    def _prefetch_related_instances(self, instance, field_source, one_to_one):
        if one_to_one:
            related_instances = [getattr(instance, field_source, None)]
        else:
            related_instances = getattr(instance, field_source, None).all()

        return {
            str(related_instance.pk): related_instance
            for related_instance in related_instances
            if related_instance
        }

    def update_or_create_reverse_relations(self, instance, reverse_relations):
        # Update or create reverse relations:
        # many-to-one, many-to-many, reversed one-to-one
        for field_name, (related_field, field, field_source) in \
                reverse_relations.items():

            # Skip processing for empty data or not-specified field.
            # The field can be defined in validated_data but isn't defined
            # in initial_data (for example, if multipart form data used)
            related_data = self.get_initial().get(field_name, None)
            if related_data is None:
                continue

            if related_field.one_to_one:
                # If an object already exists, fill in the pk so
                # we don't try to duplicate it
                pk_name = field.Meta.model._meta.pk.attname
                if pk_name not in related_data and 'pk' in related_data:
                    pk_name = 'pk'
                if pk_name not in related_data:
                    related_instance = getattr(instance, field_source, None)
                    if related_instance:
                        related_data[pk_name] = related_instance.pk

                # Expand to array of one item for one-to-one for uniformity
                related_data = [related_data]

            instances = self._prefetch_related_instances(
                instance,
                field_source,
                related_field.one_to_one,
            )

            save_kwargs = self._get_save_kwargs(field_name)
            if isinstance(related_field, GenericRelation):
                save_kwargs.update(
                    self._get_generic_lookup(instance, related_field),
                )
            elif not related_field.many_to_many:
                save_kwargs[related_field.name] = instance

            new_related_instances = []
            errors = []
            for data in related_data:
                obj = instances.get(
                    self._get_related_pk(data, field.Meta.model)
                )
                serializer = self._get_serializer_for_field(
                    field,
                    instance=obj,
                    data=data,
                )
                try:
                    serializer.is_valid(raise_exception=True)
                    related_instance = serializer.save(**save_kwargs)
                    data['pk'] = related_instance.pk
                    new_related_instances.append(related_instance)
                    errors.append({})
                except ValidationError as exc:
                    errors.append(exc.detail)

            if any(errors):
                if related_field.one_to_one:
                    raise ValidationError({field_name: errors[0]})
                else:
                    raise ValidationError({field_name: errors})

            if related_field.many_to_many:
                # Add m2m instances to through model via add
                m2m_manager = getattr(instance, field_source)
                m2m_manager.add(*new_related_instances)

    def update_or_create_direct_relations(self, attrs, relations, instance=None):
        for field_name, (related_field, field, field_source) in \
                relations.items():
            data = self.get_initial()[field_name]
            model_class = field.Meta.model
            related_instance = None
            if instance:
                related_instance = getattr(instance, field_source, None)

            # For many-to-one fields we only modify the instance if the
            # supplied pk matches, otherwise we create a new one.
            data_pk = self._get_related_pk(data, model_class)
            related_pk = str(getattr(related_instance, 'pk', None))
            if related_field.many_to_one and data_pk != related_pk:
                related_instance = None

            serializer = self._get_serializer_for_field(
                field,
                instance=related_instance,
                data=data,
            )

            try:
                serializer.is_valid(raise_exception=True)
                attrs[field_source] = serializer.save(
                    **self._get_save_kwargs(field_name)
                )
            except ValidationError as exc:
                raise ValidationError({field_name: exc.detail})

    def save(self, **kwargs):
        self._save_kwargs = defaultdict(dict, kwargs)

        return super(BaseNestedModelSerializer, self).save(**kwargs)

    def _get_save_kwargs(self, field_name):
        save_kwargs = self._save_kwargs[field_name]
        if not isinstance(save_kwargs, dict):
            raise TypeError(
                _("Arguments to nested serializer's `save` must be dict's")
            )

        return save_kwargs


class NestedCreateMixin(BaseNestedModelSerializer):
    """
    Adds nested create feature
    """
    def create(self, validated_data):
        relations, reverse_relations = self._extract_relations(validated_data)

        # Create or update direct relations (foreign key, one-to-one)
        self.update_or_create_direct_relations(
            validated_data,
            relations,
        )

        # Create instance
        instance = super(NestedCreateMixin, self).create(validated_data)

        self.update_or_create_reverse_relations(instance, reverse_relations)

        return instance


class NestedUpdateMixin(BaseNestedModelSerializer):
    """
    Adds update nested feature
    """
    default_error_messages = {
        'cannot_delete_protected': _(
            "Cannot delete {instances} because "
            "protected relation exists")
    }

    def update(self, instance, validated_data):
        relations, reverse_relations = self._extract_relations(validated_data)

        # Create or update direct relations (foreign key, one-to-one)
        self.update_or_create_direct_relations(
            validated_data,
            relations,
            instance,
        )

        # Update instance
        instance = super(NestedUpdateMixin, self).update(
            instance,
            validated_data,
        )
        self.update_or_create_reverse_relations(instance, reverse_relations)
        self.delete_reverse_relations_if_need(instance, reverse_relations)
        return instance

    def delete_reverse_relations_if_need(self, instance, reverse_relations):
        # Reverse `reverse_relations` for correct delete priority
        reverse_relations = OrderedDict(
            reversed(list(reverse_relations.items())))

        # Delete instances which are missing from data
        for field_name, (related_field, field, field_source) in \
                reverse_relations.items():

            related_data = self.get_initial()[field_name]
            related_value = getattr(instance, field_source, None)

            try:
                if related_field.one_to_one:
                    if related_data is None and related_value is not None:
                        related_value.delete()
                else:
                    related_pks = self._extract_related_pks(
                        field,
                        related_data,
                    )
                    existing_pks = {str(obj.pk) for obj in related_value.all()}
                    if existing_pks - related_pks:
                        query_set = related_value.exclude(pk__in=related_pks)
                        if related_field.many_to_many:
                            related_value.remove(*list(query_set))
                        else:
                            query_set.delete()
            except ProtectedError as e:
                instances = e.args[1]
                self.fail('cannot_delete_protected', instances=", ".join([
                    str(instance) for instance in instances]))


class UniqueFieldsMixin(serializers.ModelSerializer):
    """
    Moves `UniqueValidator`'s from the validation stage to the save stage.
    It solves the problem with nested validation for unique fields on update.

    If you want more details, you can read related issues and articles:
    https://github.com/beda-software/drf-writable-nested/issues/1
    http://www.django-rest-framework.org/api-guide/validators/#updating-nested-serializers

    Example of usage:
    ```
        class Child(models.Model):
        field = models.CharField(unique=True)


    class Parent(models.Model):
        child = models.ForeignKey('Child')


    class ChildSerializer(UniqueFieldsMixin, serializers.ModelSerializer):
        class Meta:
            model = Child


    class ParentSerializer(NestedUpdateMixin, serializers.ModelSerializer):
        child = ChildSerializer()

        class Meta:
            model = Parent
    ```

    Note: `UniqueFieldsMixin` must be applied only on the serializer
    which has unique fields.

    Note: When you are using both mixins
    (`UniqueFieldsMixin` and `NestedCreateMixin` or `NestedUpdateMixin`)
    you should put `UniqueFieldsMixin` ahead.
    """
    _unique_fields = []

    def get_fields(self):
        self._unique_fields = []

        fields = super(UniqueFieldsMixin, self).get_fields()
        for field_name, field in fields.items():
            is_unique = any([isinstance(validator, UniqueValidator)
                             for validator in field.validators])
            if is_unique:
                self._unique_fields.append(field_name)
                field.validators = [
                    validator for validator in field.validators
                    if not isinstance(validator, UniqueValidator)]

        return fields

    def _validate_unique_fields(self, validated_data):
        for field_name in self._unique_fields:
            unique_validator = UniqueValidator(self.Meta.model.objects.all())
            try:
                # `set_context` removed on DRF >= 3.11, pass in via __call__ instead
                if hasattr(unique_validator, 'set_context'):
                    unique_validator.set_context(self.fields[field_name])
                    unique_validator(validated_data[field_name])
                else:
                    unique_validator(validated_data[field_name], self.fields[field_name])
            except ValidationError as exc:
                raise ValidationError({field_name: exc.detail})

    def create(self, validated_data):
        self._validate_unique_fields(validated_data)
        return super(UniqueFieldsMixin, self).create(validated_data)

    def update(self, instance, validated_data):
        self._validate_unique_fields(validated_data)
        return super(UniqueFieldsMixin, self).update(instance, validated_data)
