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
    "contract_price_avg": {
        "aliases": ["میانگین قیمت قرارداد"],
        "expression": "AVG(Contract.Price)"
    },
    "contract_price_max": {
        "aliases": ["بیشترین قیمت قرارداد"],
        "expression": "MAX(Contract.Price)"
    },
    "hall_matching_quantity": {
        "aliases": ["حجم مچینگ"],
        "expression": "SUM(Contract.HallMatchingQuantity)"
    },
    "hall_matching_value": {
        "aliases": ["ارزش مچینگ"],
        "expression": "SUM(Contract.HallMatchingTotalPrice)"
    },
    "customer_contract_value": {
        "aliases": ["ارزش قرارداد مشتری"],
        "expression": "SUM(CustomerContract.TotalPrice)"
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
    "offer_quantity": {
        "aliases": ["مقدار عرضه"],
        "expression": "SUM(Offer.OfferQuantity)"
    },
    "offer_price_avg": {
        "aliases": ["میانگین قیمت عرضه"],
        "expression": "AVG(Offer.OfferPrice)"
    },
    "hall_contract_value": {
        "aliases": ["ارزش قراردادهای تالار"],
        "expression": "SUM(Offer.HallContractTotalPrice)"
    }
}
