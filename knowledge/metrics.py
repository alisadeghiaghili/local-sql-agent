METRICS = {

    "purchase_value": {
        "aliases": ["ارزش خرید", "مبلغ خرید"],
        "expression": "SUM(CustomerContract.TotalPrice)"
    },

    "purchase_volume": {
        "aliases": ["حجم خرید"],
        "expression": "SUM(CustomerContract.Quantity)"
    },

    "trade_value": {
        "aliases": ["ارزش معامله"],
        "expression": "SUM(Contract.TotalPrice)"
    },

    "contract_price_sum": {
        "aliases": ["جمع قیمت معاملات"],
        "expression": "SUM(Contract.Price)"
    },

    "contract_price_avg": {
        "aliases": ["میانگین قیمت قرارداد"],
        "expression": "AVG(Contract.Price)"
    },

    "contract_price_max": {
        "aliases": ["بیشترین قیمت قرارداد"],
        "expression": "MAX(Contract.Price)"
    },

    "contract_price_min": {
        "aliases": ["کمترین قیمت قرارداد"],
        "expression": "MIN(Contract.Price)"
    },

    "hall_matching_quantity": {
        "aliases": ["حجم مچینگ"],
        "expression": "SUM(Contract.HallMatchingQuantity)"
    },

    "hall_matching_weight": {
        "aliases": ["وزن مچینگ"],
        "expression": "SUM(Contract.HallMatchingWeight)"
    },

    "hall_matching_value": {
        "aliases": ["ارزش مچینگ"],
        "expression": "SUM(Contract.HallMatchingTotalPrice)"
    },

    "customer_contract_value": {
        "aliases": ["ارزش قرارداد مشتری"],
        "expression": "SUM(CustomerContract.TotalPrice)"
    },

    "customer_contract_quantity": {
        "aliases": ["مقدار قرارداد مشتری"],
        "expression": "SUM(CustomerContract.Quantity)"
    },

    "buy_broker_wage": {
        "aliases": ["کارمزد خرید"],
        "expression": "SUM(CustomerContract.BuyBrokerWage)"
    },

    "sell_broker_wage": {
        "aliases": ["کارمزد فروش"],
        "expression": "SUM(CustomerContract.SellBrokerWage)"
    },

    "buy_ime_wage": {
        "aliases": ["کارمزد خرید بورس"],
        "expression": "SUM(CustomerContract.BuyIMEWage)"
    },

    "sell_ime_wage": {
        "aliases": ["کارمزد فروش بورس"],
        "expression": "SUM(CustomerContract.SellIMEWage)"
    },

    "buy_seo_wage": {
        "aliases": ["کارمزد خرید سازمان"],
        "expression": "SUM(CustomerContract.BuySEOWage)"
    },

    "sell_seo_wage": {
        "aliases": ["کارمزد فروش سازمان"],
        "expression": "SUM(CustomerContract.SellSEOWage)"
    },

    "offer_quantity": {
        "aliases": ["مقدار عرضه"],
        "expression": "SUM(Offer.OfferQuantity)"
    },

    "offer_price_avg": {
        "aliases": ["میانگین قیمت عرضه"],
        "expression": "AVG(Offer.OfferPrice)"
    },

    "offer_price_max": {
        "aliases": ["بیشترین قیمت عرضه"],
        "expression": "MAX(Offer.OfferMaxPrice)"
    },

    "hall_sale_quantity": {
        "aliases": ["حجم فروش تالار"],
        "expression": "SUM(Offer.HallSaleQuantity)"
    },

    "hall_purchase_quantity": {
        "aliases": ["حجم تقاضا"],
        "expression": "SUM(Offer.HallPurchaseQuantity)"
    },

    "hall_contract_count": {
        "aliases": ["تعداد قراردادهای تالار"],
        "expression": "SUM(Offer.HallContractCount)"
    },

    "hall_contract_value": {
        "aliases": ["ارزش قراردادهای تالار"],
        "expression": "SUM(Offer.HallContractTotalPrice)"
    },

    "extra_contract_count": {
        "aliases": ["تعداد قرارداد مچینگ"],
        "expression": "SUM(Offer.ExtraContractCount)"
    },

    "extra_contract_value": {
        "aliases": ["ارزش قرارداد مچینگ"],
        "expression": "SUM(Offer.ExtraContractTotalPrice)"
    },

    "last_contract_price": {
        "aliases": ["آخرین قیمت معامله"],
        "expression": "MAX(Offer.LastContractPrice)"
    },

    "order_lot": {
        "aliases": ["حجم سفارش"],
        "expression": "SUM(Order.OrderLot)"
    },

    "order_price_avg": {
        "aliases": ["میانگین قیمت سفارش"],
        "expression": "AVG(Order.BuyOrder_Price)"
    },

    "order_price_max": {
        "aliases": ["بیشترین قیمت سفارش"],
        "expression": "MAX(Order.BuyOrder_Price)"
    },

    "order_price_min": {
        "aliases": ["کمترین قیمت سفارش"],
        "expression": "MIN(Order.BuyOrder_Price)"
    },

    "order_value": {
        "aliases": ["ارزش سفارشات"],
        "expression": "SUM(Order.OrderLot * Order.BuyOrder_Price)"
    },

    "talarlog_new_price": {
        "aliases": ["قیمت جدید"],
        "expression": "AVG(TalarLog.NewPrice)"
    },

    "talarlog_old_price": {
        "aliases": ["قیمت قبلی"],
        "expression": "AVG(TalarLog.OldPrice)"
    },

    "talarlog_new_amount": {
        "aliases": ["حجم جدید"],
        "expression": "SUM(TalarLog.NewAmount)"
    },

    "talarlog_old_amount": {
        "aliases": ["حجم قبلی"],
        "expression": "SUM(TalarLog.OldAmount)"
    }
}
