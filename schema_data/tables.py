TABLE_DESCRIPTIONS = {

    # --- Fact Tables ---
    "Contract": "Auction_Fact.Contract — trade contracts (transactions)",
    "CustomerContract": "Auction_Fact.CustomerContract — customer purchase records",
    "Offer": "Auction_Fact.Offer — supply offers submitted by suppliers",
    "Order": "Auction_Fact.Order — purchase orders placed by buyers",
    "TalarLog": "Auction_Fact.TalarLog — operational log of trading hall events",

    # --- Dimension Tables ---
    "Customer": "Auction_Dim.Customer — buyer / customer master data",
    "Supplier": "Auction_Dim.Supplier — supplier / seller master data",
    "Broker": "Auction_Dim.Broker — brokerage firms",
    "Symbol": "Auction_Dim.Symbol — trading symbols (commodities)",
    "Ring": "Auction_Dim.Ring — trading halls / rings",
    "Date": "General_Dim.Date — Persian calendar date dimension",
    "Currency": "Auction_Dim.Currency — currency master data",
    "Bank": "Auction_Dim.Bank — bank master data",
    "Carrier": "Auction_Dim.Carrier — logistics / transport companies",
    "ContractKind": "Auction_Dim.ContractKind — contract type (cash, forward, etc.)",
    "ContractStatus": "Auction_Dim.ContractStatus — contract lifecycle status",
    "OfferStatus": "Auction_Dim.OfferStatus — offer lifecycle status",
    "OfferKind": "Auction_Dim.OfferKind — offer type classification",
    "DeliveryPlace": "Auction_Dim.DeliveryPlace — delivery location / warehouse",
    "PaymentDelivery": "Auction_Dim.PaymentDelivery — payment & delivery terms",
    "ClearingKind": "Auction_Dim.ClearingKind — settlement / clearing type",
    "BuyMethod": "Auction_Dim.BuyMethod — purchase method classification",
    "GeneralStatus": "Auction_Dim.GeneralStatus — generic status lookup",
}
