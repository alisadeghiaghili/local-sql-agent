RELATIONSHIPS = {

    # =========================
    # CONTRACT
    # =========================

    "Contract.Symbol_ID -> Symbol.ID":
        "[Auction_Fact].[Contract].[Symbol_ID] = [Auction_Dim].[Symbol].[ID]",

    "Contract.Supplier_ID -> Supplier.ID":
        "[Auction_Fact].[Contract].[Supplier_ID] = [Auction_Dim].[Supplier].[ID]",

    "Contract.BuyerBroker_ID -> Broker.ID":
        "[Auction_Fact].[Contract].[BuyerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "Contract.SellerBroker_ID -> Broker.ID":
        "[Auction_Fact].[Contract].[SellerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "Contract.Date_ID -> Date.ID":
        "[Auction_Fact].[Contract].[Date_ID] = [General_Dim].[Date].[ID]",

    "Contract.Ring_ID -> Ring.ID":
        "[Auction_Fact].[Contract].[Ring_ID] = [Auction_Dim].[Ring].[ID]",

    "Contract.ContractKind_ID -> ContractKind.ID":
        "[Auction_Fact].[Contract].[ContractKind_ID] = [Auction_Dim].[ContractKind].[ID]",

    "Contract.Currency_ID -> Currency.ID":
        "[Auction_Fact].[Contract].[Currency_ID] = [Auction_Dim].[Currency].[ID]",

    "Contract.ContractStatus_ID -> ContractStatus.ID":
        "[Auction_Fact].[Contract].[ContractStatus_ID] = [Auction_Dim].[ContractStatus].[ID]",

    "Contract.OfferKind_ID -> OfferKind.ID":
        "[Auction_Fact].[Contract].[OfferKind_ID] = [Auction_Dim].[OfferKind].[ID]",

    # =========================
    # CUSTOMER CONTRACT
    # =========================

    "CustomerContract.BuyerCustomer_ID -> Customer.ID":
        "[Auction_Fact].[CustomerContract].[BuyerCustomer_ID] = [Auction_Dim].[Customer].[ID]",

    "CustomerContract.Contract_ID -> Contract.ID":
        "[Auction_Fact].[CustomerContract].[Contract_ID] = [Auction_Fact].[Contract].[ID]",

    "CustomerContract.Symbol_ID -> Symbol.ID":
        "[Auction_Fact].[CustomerContract].[Symbol_ID] = [Auction_Dim].[Symbol].[ID]",

    "CustomerContract.Supplier_ID -> Supplier.ID":
        "[Auction_Fact].[CustomerContract].[Supplier_ID] = [Auction_Dim].[Supplier].[ID]",

    "CustomerContract.BuyerBroker_ID -> Broker.ID":
        "[Auction_Fact].[CustomerContract].[BuyerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "CustomerContract.SellerBroker_ID -> Broker.ID":
        "[Auction_Fact].[CustomerContract].[SellerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "CustomerContract.Date_ID -> Date.ID":
        "[Auction_Fact].[CustomerContract].[Date_ID] = [General_Dim].[Date].[ID]",

    "CustomerContract.Ring_ID -> Ring.ID":
        "[Auction_Fact].[CustomerContract].[Ring_ID] = [Auction_Dim].[Ring].[ID]",

    "CustomerContract.ContractKind_ID -> ContractKind.ID":
        "[Auction_Fact].[CustomerContract].[ContractKind_ID] = [Auction_Dim].[ContractKind].[ID]",

    "CustomerContract.Currency_ID -> Currency.ID":
        "[Auction_Fact].[CustomerContract].[Currency_ID] = [Auction_Dim].[Currency].[ID]",

    "CustomerContract.Bank_ID -> Bank.ID":
        "[Auction_Fact].[CustomerContract].[Bank_ID] = [Auction_Dim].[Bank].[ID]",

    "CustomerContract.Carrier_ID -> Carrier.ID":
        "[Auction_Fact].[CustomerContract].[Carrier_ID] = [Auction_Dim].[Carrier].[ID]",

    "CustomerContract.ClearingKind_ID -> ClearingKind.ID":
        "[Auction_Fact].[CustomerContract].[ClearingKind_ID] = [Auction_Dim].[ClearingKind].[ID]",

    "CustomerContract.ContractStatus_ID -> ContractStatus.ID":
        "[Auction_Fact].[CustomerContract].[ContractStatus_ID] = [Auction_Dim].[ContractStatus].[ID]",

    "CustomerContract.OfferKind_ID -> OfferKind.ID":
        "[Auction_Fact].[CustomerContract].[OfferKind_ID] = [Auction_Dim].[OfferKind].[ID]",

    # =========================
    # OFFER
    # =========================

    "Offer.Symbol_ID -> Symbol.ID":
        "[Auction_Fact].[Offer].[Symbol_ID] = [Auction_Dim].[Symbol].[ID]",

    "Offer.Supplier_ID -> Supplier.ID":
        "[Auction_Fact].[Offer].[Supplier_ID] = [Auction_Dim].[Supplier].[ID]",

    "Offer.Broker_ID -> Broker.ID":
        "[Auction_Fact].[Offer].[Broker_ID] = [Auction_Dim].[Broker].[ID]",

    "Offer.Date_ID -> Date.ID":
        "[Auction_Fact].[Offer].[Date_ID] = [General_Dim].[Date].[ID]",

    "Offer.Ring_ID -> Ring.ID":
        "[Auction_Fact].[Offer].[Ring_ID] = [Auction_Dim].[Ring].[ID]",

    "Offer.ContractKind_ID -> ContractKind.ID":
        "[Auction_Fact].[Offer].[ContractKind_ID] = [Auction_Dim].[ContractKind].[ID]",

    "Offer.Currency_ID -> Currency.ID":
        "[Auction_Fact].[Offer].[Currency_ID] = [Auction_Dim].[Currency].[ID]",

    "Offer.PaymentDelivery_ID -> PaymentDelivery.ID":
        "[Auction_Fact].[Offer].[PaymentDelivery_ID] = [Auction_Dim].[PaymentDelivery].[ID]",

    "Offer.DeliveryPlace_ID -> DeliveryPlace.ID":
        "[Auction_Fact].[Offer].[DeliveryPlace_ID] = [Auction_Dim].[DeliveryPlace].[ID]",

    "Offer.OfferStatus_ID -> OfferStatus.ID":
        "[Auction_Fact].[Offer].[OfferStatus_ID] = [Auction_Dim].[OfferStatus].[ID]",

    "Offer.OfferItemStatus_ID -> OfferItemStatus.ID":
        "[Auction_Fact].[Offer].[OfferItemStatus_ID] = [Auction_Dim].[OfferItemStatus].[ID]",

    "Offer.OfferKind_ID -> OfferKind.ID":
        "[Auction_Fact].[Offer].[OfferKind_ID] = [Auction_Dim].[OfferKind].[ID]",

    "Offer.BuyMethod_ID -> BuyMethod.ID":
        "[Auction_Fact].[Offer].[BuyMethod_ID] = [Auction_Dim].[BuyMethod].[ID]",

    # =========================
    # ORDER
    # =========================

    "Order.BuyerCustomer_ID -> Customer.ID":
        "[Auction_Fact].[Order].[BuyerCustomer_ID] = [Auction_Dim].[Customer].[ID]",

    "Order.BuyerBroker_ID -> Broker.ID":
        "[Auction_Fact].[Order].[BuyerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "Order.SellerBroker_ID -> Broker.ID":
        "[Auction_Fact].[Order].[SellerBroker_ID] = [Auction_Dim].[Broker].[ID]",

    "Order.Supplier_ID -> Supplier.ID":
        "[Auction_Fact].[Order].[Supplier_ID] = [Auction_Dim].[Supplier].[ID]",

    "Order.Symbol_ID -> Symbol.ID":
        "[Auction_Fact].[Order].[Symbol_ID] = [Auction_Dim].[Symbol].[ID]",

    "Order.Ring_ID -> Ring.ID":
        "[Auction_Fact].[Order].[Ring_ID] = [Auction_Dim].[Ring].[ID]",

    "Order.Date_ID -> Date.ID":
        "[Auction_Fact].[Order].[Date_ID] = [General_Dim].[Date].[ID]",

    "Order.Offer_ID -> Offer.ID":
        "[Auction_Fact].[Order].[Offer_ID] = [Auction_Fact].[Offer].[ID]"
}
