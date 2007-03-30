# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005-2007 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s):   Henrique Romano         <henrique@async.com.br>
##              Evandro Vale Miquelito  <evandro@async.com.br>
##              Johan Dahlin            <jdahlin@async.com.br>
##
"""FiscalPrinting (ECF) integration."""

from decimal import Decimal

from kiwi.argcheck import argcheck
from kiwi.log import Logger
from zope.interface import implements
from stoqdrivers.enum import PaymentMethodType, TaxType, UnitType
from stoqdrivers.exceptions import (CouponOpenError, DriverError,
                                    CouponNotOpenError)

from stoqlib.database.runtime import new_transaction, get_current_station
from stoqlib.domain.devices import DeviceSettings
from stoqlib.domain.giftcertificate import GiftCertificateItem
from stoqlib.domain.interfaces import (IIndividual, ICompany, IPaymentGroup,
                                       IContainer)
from stoqlib.domain.payment.methods import CheckPM, MoneyPM
from stoqlib.domain.sale import Sale
from stoqlib.domain.sellable import ASellableItem
from stoqlib.exceptions import DeviceError
from stoqlib.lib.defaults import (METHOD_GIFT_CERTIFICATE, get_all_methods_dict,
                                  get_method_names)
from stoqlib.lib.message import warning
from stoqlib.lib.translation import stoqlib_gettext

_ = stoqlib_gettext

log = Logger("stoqlib.drivers.fiscalprinter")


#
# Public API
#


def get_fiscal_printer_settings_by_station(conn, station):
    """ Returns the DeviceSettings object representing the printer currently
    associated with the given station or None if there is not settings for
    it.
    """
    return DeviceSettings.selectOneBy(
        connection=conn,
        station=station,
        type=DeviceSettings.FISCAL_PRINTER_DEVICE)

def create_virtual_printer(trans, station):
    """
    Create a virtual printer for a station
    @param trans: a database transaction
    @param station: a station
    """
    settings = DeviceSettings(station=station,
                              device=DeviceSettings.DEVICE_SERIAL1,
                              brand='virtual',
                              model='Simple',
                              type=DeviceSettings.FISCAL_PRINTER_DEVICE,
                              connection=trans)
    settings.create_fiscal_printer_constants()

def create_virtual_printer_for_current_station():
    trans = new_transaction()
    station = get_current_station(trans)
    if get_fiscal_printer_settings_by_station(trans, station):
        trans.close()
        return
    create_virtual_printer(trans, station)
    trans.commit(close=True)

#
# Coupon & Cheque
#





class CouponPrinter(object):
    """
    CouponPrinter is a wrapper around the FiscalPrinter class inside
    stoqdrivers, refer to it for documentation
    """
    def __init__(self, driver, settings):
        self._driver = driver
        self._settings = settings

    def open_till(self, value=0):
        """
        Opens the till
        @param value: optional, how much to add to the till
          after opening it
        """
        log.info("Opening till")

        self._driver.summarize()

        assert value >= 0

        if value > 0:
            self.add_cash(value)

    def close_till(self, value=0):
        """
        Closes the till
        @param value: optional, how much to remove from the till
          before closing it
        """
        log.info("Closing till")

        assert value >= 0

        if value > 0:
            self.remove_cash(value)

        self._driver.close_till()

    def cancel(self):
        """
        Cancel the current or the last made sale.
        @return: True it was canceled, False if there was nothing to cancel
        """
        # FIXME: We need to ask the fiscal printer which was the last
        #        made sale and cancel the sale with /that/ coupon number
        #        That requires each sale to have a reference to a coupon.
        #        See #3130 for more information

        try:
            self._driver.cancel()
        except CouponNotOpenError:
            return False

        trans = new_transaction()

        sale = Sale.get_last_confirmed(trans)
        if not sale:
            return False
        sale.cancel(sale.create_sale_return_adapter())

        trans.commit(close=True)

        return True

    def add_cash(self, value):
        """
        Remove cash to the till
        @param value: a positive value indicating how much to add
        """
        assert value > 0
        self._driver.till_add_cash(value)

    def remove_cash(self, value):
        """
        Remove cash from the till
        @param value: a positive value indicating how much to remove
        """
        assert value > 0
        self._driver.till_remove_cash(value)

    def emit_coupon(self, sale):
        """ Emit a coupon for a Sale instance.

        @returns: True if the coupon has been emitted, False otherwise.
        """
        products = sale.get_products()
        if not products:
            return True

        coupon = self.create_coupon(sale)
        if sale.client:
            coupon.identify_customer(sale.client.person)
        if not coupon.open():
            return False
        for product in products:
            coupon.add_item(product)
        if not coupon.totalize():
            return False
        if not coupon.setup_payments():
            return False
        return coupon.close()

    def create_coupon(self, sale):
        """
        @param sale: a L{stoqlib.domain.sale.Sale}
        @returns: a new coupon
        """
        return FiscalCoupon(self._driver, self._settings, sale)



#
# Class definitions
#


class FiscalCoupon(object):
    """ This class is used just to allow us cancel an item with base in a
    ASellable object. Currently, services can't be added, and they
    are just ignored -- be aware, if a coupon with only services is
    emitted, it will not be opened in fact, but just ignored.
    """
    implements(IContainer)

    def __init__(self, driver, settings, sale):
        self._driver = driver
        self._settings = settings
        self._sale = sale
        self._item_ids = {}

    def _get_capability(self, name):
        return self._driver.get_capabilities()[name]

    #
    # IContainer implementation
    #

    @argcheck(ASellableItem)
    def add_item(self, item):
        """
        @param item: A L{ASellableItem} subclass
        @returns: id of the item.:
          0 >= if it was added successfully
          -1 if an error happend
          0 if added but not printed (gift certificates, free deliveries)
        """
        # GiftCertificates are not printed on the fiscal printer
        # See #2985 for more information.
        if isinstance(item, GiftCertificateItem):
            return 0

        if item.price <= 0:
            return 0
        sellable = item.sellable
        max_len = self._get_capability("item_description").max_len
        description = sellable.base_sellable_info.description[:max_len]
        unit_desc = ''
        if not sellable.unit:
            unit = UnitType.EMPTY
        else:
            if sellable.unit.unit_index == UnitType.CUSTOM:
                unit_desc = sellable.unit.description
            unit = sellable.unit.unit_index or UnitType.EMPTY
        max_len = self._get_capability("item_code").max_len
        code = sellable.get_code_str()[:max_len]

        try:
            tax_constant = self._settings.get_tax_constant_for_device(sellable)
        except DeviceError, e:
            warning(_("Could not print item"), str(e))
            return -1
        item_id = self._driver.add_item(code, description, item.price,
                                        tax_constant.device_value,
                                        item.quantity, unit,
                                        unit_desc=unit_desc)
        ids = self._item_ids.setdefault(item, [])
        ids.append(item_id)
        return item_id

    def get_items(self):
        return self._item_ids.keys()

    @argcheck(ASellableItem)
    def remove_item(self, item):
        if isinstance(item, GiftCertificateItem):
            return
        if item.price <= 0:
            return
        for item_id in self._item_ids.pop(item):
            try:
                self._driver.cancel_item(item_id)
            except DriverError:
                return False
        return True

    #
    # Fiscal coupon related functions
    #

    def identify_customer(self, person):
        max_len = self._get_capability("customer_id").max_len
        if IIndividual(person):
            individual = IIndividual(person)
            document = individual.cpf[:max_len]
        elif ICompany(person):
            company = ICompany(person)
            document = company.cnpj[:max_len]
        else:
            raise TypeError(
                "identify_customer needs an object implementing "
                "IIndividual or ICompany")
        max_len = self._get_capability("customer_name").max_len
        name = person.name[:max_len]
        max_len = self._get_capability("customer_address").max_len
        address = person.get_address_string()[:max_len]
        self._driver.identify_customer(name, address, document)

    def open(self):
        try:
            self._driver.open()
        except CouponOpenError:
            if not self.cancel():
                return False
        return True

    def totalize(self):
        # XXX: Remove this when bug #2827 is fixed.
        if not self._item_ids:
            return True

        # Surcharge is currently disabled, see #2811
        #if discount > surcharge:
        #    discount = discount - surcharge
        #    surcharge = 0
        #elif surcharge > discount:
        #    surcharge = surcharge - discount
        #    discount = 0
        #else:
        #    # If these values are greater than zero we will get problems in
        #    # stoqdrivers
        #    surcharge = discount = 0
        surcharge = Decimal('0')

        discount = self._sale.discount_percentage

        try:
            self._driver.totalize(discount, surcharge, TaxType.NONE)
        except DriverError, details:
            warning(_(u"It is not possible to totalize the coupon"),
                    str(details))
            return False
        return True

    def cancel(self):
        try:
            self._driver.cancel()
        except DriverError:
            return False
        return True

    # FIXME: Rename to add_payment_group(group)
    def setup_payments(self):
        """ Add the payments defined in the sale to the coupon. Note that this
        function must be called after all the payments has been created.
        """
        log.info("setting up payments")

        # XXX: Remove this when bug #2827 is fixed.
        if not self._item_ids:
            return True
        sale = self._sale
        group = IPaymentGroup(sale)
        if group.default_method == METHOD_GIFT_CERTIFICATE:
            self._driver.add_payment(PaymentMethodType.MONEY,
                                     sale.get_total_sale_amount())
            return True

        log.info("we have %d payments" % (group.get_items().count()),)
        all_methods = get_all_methods_dict().items()
        method_id = None
        for payment in group.get_items():
            method = payment.method
            if isinstance(method, (CheckPM, MoneyPM)):
                if isinstance(method, CheckPM):
                    method_id = PaymentMethodType.CHECK
                else:
                    method_id = PaymentMethodType.MONEY
                self._driver.add_payment(method_id, payment.base_value)
                continue
            method_type = type(method)
            method_id = None
            for identifier, mtype in all_methods:
                if method_type == mtype:
                    if method_id is not None:
                        raise TypeError(
                            "There is the same identifier for two "
                            "different payment method interfaces. "
                            "The identifier is %d" % method_id)
                    method_id = identifier
            if method_id is None:
                raise ValueError(
                    "Can't find a valid identifier for the payment "
                    "method type: %s. It is not possible add "
                    "the payment on the coupon" %
                    method_type.__name__)

            constant = self._settings.get_payment_constant(method_id)
            if not constant:
                method_name = get_method_names()[method_id]
                raise DeviceError(
                    _(u"The payment method used in this sale (%s) is not "
                      "configured in the fiscal printer." % method_name))

            self._driver.add_payment(PaymentMethodType.CUSTOM, payment.base_value,
                                     custom_pm=constant.device_value)

        return True

    def close(self):
        # XXX: Remove this when bug #2827 is fixed.
        if not self._item_ids:
            return True
        try:
            coupon_id = self._driver.close()
        except DriverError, details:
            warning(_("It's not possible to close the coupon"), str(details))
            return False
        self._sale.coupon_id = coupon_id
        return True

