# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2006-2007 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s):     Henrique Romano <henrique@async.com.br>
##                Johan Dahlin <jdahlin@async.com.br>
##

from decimal import Decimal

from stoqdrivers.exceptions import DriverError
from stoqlib.database.runtime import get_current_station
from stoqlib.domain.interfaces import IPaymentGroup, ISellable
from stoqlib.domain.payment.methods import BillPM, CheckPM, MoneyPM
from stoqlib.domain.test.domaintest import DomainTest
from stoqlib.drivers.fiscalprinter import (
    get_fiscal_printer_settings_by_station)
from stoqdrivers.exceptions import CouponOpenError
from stoqlib.lib.defaults import METHOD_BILL, METHOD_CHECK, METHOD_MONEY

class TestDrivers(DomainTest):

    def testVirtualPrinterCreation(self):
        station = get_current_station(self.trans)
        settings = get_fiscal_printer_settings_by_station(self.trans,
                                                          station)
        self.failUnless(settings is not None, ("You should have a valid "
                                               "printer at this point."))


class TestCouponPrinter(DomainTest):
    def setUp(self):
        DomainTest.setUp(self)
        self.printer = self.create_coupon_printer()

    def testCloseTill(self):
        self.printer.close_till(Decimal(0))
        self.assertRaises(DriverError, self.printer.close_till, 0)

    def testEmitCoupon(self):
        sale = self.create_sale()
        self.printer.emit_coupon(sale)

    def testAddCash(self):
        self.printer.add_cash(Decimal(100))

    def testRemoveCash(self):
        self.printer.remove_cash(Decimal(100))

    def testCancel(self):
        self.printer.cancel()

class TestFiscalCoupon(DomainTest):
    def setUp(self):
        DomainTest.setUp(self)
        self.printer = self.create_coupon_printer()
        self.sale = self.create_sale()
        self.coupon = self.printer.create_coupon(self.sale)

    def testAddItemProduct(self):
        product = self.create_product()
        sellable = ISellable(product)
        item = sellable.add_sellable_item(self.sale)

        self.assertRaises(CouponOpenError, self.coupon.add_item, item)

        self.coupon.open()
        self.coupon.add_item(item)

    def testAddItemService(self):
        service = self.create_service()
        sellable = ISellable(service)
        item = sellable.add_sellable_item(self.sale)

        self.coupon.open()
        self.coupon.add_item(item)

class _TestFiscalCouponPayments:
    def setUp(self):
        DomainTest.setUp(self)
        self.printer = self.create_coupon_printer()
        self.sale = self.create_sale()
        self.coupon = self.printer.create_coupon(self.sale)

    def _open_and_add(self, product):
        sellable = ISellable(product)
        item = sellable.add_sellable_item(self.sale)

        self.coupon.open()
        self.coupon.add_item(item)
        self.coupon.totalize()

    def _add_sale_payments(self, sale, constant, method_type):
        group = sale.addFacet(IPaymentGroup,
                              connection=self.trans,
                              default_method=constant,
                              installments_number=1)

        method = method_type.selectOne(connection=self.trans)
        method.create_inpayment(group, sale.get_total_sale_amount())
        self.sale.set_valid()

    def testSetupPayment(self):
        product = self.create_product()
        self._open_and_add(product)
        self._add_sale_payments(self.sale, self.constant, self.method)
        self.coupon.setup_payments()

class TestFiscalCouponPaymentsBill(DomainTest, _TestFiscalCouponPayments):
    setUp = _TestFiscalCouponPayments.setUp
    method = BillPM
    constant = METHOD_BILL

class TestFiscalCouponPaymentsCheck(DomainTest, _TestFiscalCouponPayments):
    setUp = _TestFiscalCouponPayments.setUp
    method = CheckPM
    constant = METHOD_CHECK

class TestFiscalCouponPaymentsMoney(DomainTest, _TestFiscalCouponPayments):
    setUp = _TestFiscalCouponPayments.setUp
    method = MoneyPM
    constant = METHOD_MONEY
