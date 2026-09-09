const q = (s) => document.querySelector(s),
  qa = (s) => [...document.querySelectorAll(s)],
  nodes = [
    ".eyebrow",
    "h1",
    ".lede",
    ".intro .section-kicker",
    ".intro h2",
    ".intro h2 + p",
    ".architecture .section-kicker",
    ".architecture h2",
    ".architecture-head > p:last-child",
    ".start .section-kicker",
    ".start h2",
    ".start > div > p:not(.section-kicker)",
    ".start .button",
    ".install-top span",
  ];
const en = Object.fromEntries(nodes.map((s) => [s, q(s).innerHTML]));
const nav = qa(".topbar nav a").map((x) => x.innerHTML),
  actions = qa(".hero-actions a").map((x) => x.innerHTML),
  proof = qa(".hero-proof span").map((x) => x.innerHTML),
  cards = qa(".principles article").map((x) => [
    x.querySelector("h3").innerHTML,
    x.querySelector("p").innerHTML,
  ]),
  features = qa(".feature").map((x) => [
    x.querySelector("h3").innerHTML,
    x.querySelector("p").innerHTML,
  ]),
  source = q(".source-link").innerHTML,
  footer = qa("footer span")[1].innerHTML;
let lang = "en",
  i = 0;
const examples = [
  [
    "Which 10 customers had the highest purchase value in 1403?",
    "۱۰ مشتری برتر از نظر مبلغ خرید در سال ۱۴۰۳ کدام‌اند؟",
  ],
  [
    "What were monthly sales for home appliances in 1403?",
    "فروش ماهانه دسته لوازم خانگی در ۱۴۰۳ چقدر بوده است؟",
  ],
  [
    "Which regions have the highest order growth?",
    "کدام منطقه‌ها بیشترین رشد سفارش را داشته‌اند؟",
  ],
];
const sql = [
  '<span class="kw">SELECT TOP</span> <span class="num">10</span><br>c.Name, <span class="fn">SUM</span>(o.TotalAmount) <span class="kw">AS</span> PurchaseValue<br><span class="kw">FROM</span> Sales_Fact.<span class="table">Order</span> o<br><span class="kw">WHERE</span> d.JalaliYear = <span class="num">1403</span><br><span class="kw">ORDER BY</span> PurchaseValue <span class="kw">DESC</span>',
  '<span class="kw">SELECT</span> d.JalaliMonthName, <span class="fn">SUM</span>(o.TotalAmount) <span class="kw">AS</span> SalesValue<br><span class="kw">FROM</span> Sales_Fact.<span class="table">Order</span> o<br><span class="kw">WHERE</span> d.JalaliYear = <span class="num">1403</span>',
  '<span class="kw">SELECT</span> r.RegionName, GrowthRate<br><span class="kw">FROM</span> Analytics.<span class="table">RegionalGrowth</span> r<br><span class="kw">ORDER BY</span> GrowthRate <span class="kw">DESC</span>',
];
const fa = {
  ".eyebrow": "<i></i> حریم خصوصی در معماری",
  h1: "به زبان خودت بپرس.<br><em>با قواعد خودت کوئری بگیر.</em>",
  ".lede":
    "یک عامل SQL مبتنی بر زبان طبیعی برای انبارداده‌های حساس. پرسش فارسی یا انگلیسی وارد می‌شود؛ SQL امن و قابل بررسی خارج می‌شود. بدون وابستگی ابری و بدون خروج داده از ماشین شما.",
  ".intro .section-kicker": "۰۱ / چرا ساخته شد",
  ".intro h2": "Text-to-SQL ابری، انتخاب پیش‌فرض بدی برای داده حساس است.",
  ".intro h2 + p":
    "انبارداده، اصطلاحات، قواعد دسترسی و نقطه اتصال مدل همگی تحت کنترل شما می‌مانند. این ابزار تحلیل زبانی را به یک فرایند داده‌ای حاکمیت‌پذیر تبدیل می‌کند؛ نه یک جعبه سیاه.",
  ".architecture .section-kicker": "۰۲ / موتور",
  ".architecture h2": "زمینه کوچک.<br><em>پاسخ‌های قوی.</em>",
  ".architecture-head > p:last-child":
    "موتور به‌جای ریختن کل انبارداده در context مدل، فقط آنچه پرسش فعلی لازم دارد را انتخاب می‌کند.",
  ".start .section-kicker": "۰۳ / شروع کنید",
  ".start h2": "داده را همان‌جایی نگه دارید<br><em>که باید باشد.</em>",
  ".start > div > p:not(.section-kicker)":
    "عامل را به انبارداده خود وصل کنید، واژگان دامنه‌تان را پیکربندی کنید و همه‌چیز را داخل محیط خودتان اجرا کنید.",
  ".start .button": "خواندن مستندات <span>↗</span>",
  ".install-top span": "شروع سریع",
};
const fcards = [
  [
    "دامنه شما<br>یک پرامپت نیست.",
    "اسکیما، نام‌های جایگزین، متریک‌ها، قواعد و مثال‌ها به‌صورت YAML قابل‌انتقال و خارج از موتور نگهداری می‌شوند.",
  ],
  [
    "امنیت<br>صرفاً یک شعار نیست.",
    "allowlist مبتنی بر AST و ACL ستون‌ها، SQL تولیدشده را پیش از اجرا اعتبارسنجی می‌کنند.",
  ],
  [
    "زمینه باید<br>تجمعی باشد.",
    "سؤال‌های ادامه‌دار در برابر SQL قبلی به‌شکل CTE ساخته می‌شوند و فرض‌ها شفاف باقی می‌مانند.",
  ],
];
const ffeatures = [
  [
    "مدل محلی خودت را وصل کن",
    "نقطه اتصال سازگار با OpenAI مانند Ollama، LM Studio، vLLM یا سرور خودتان را وصل کنید.",
  ],
  [
    "برای تحلیل دوزبانه ساخته شده",
    "پرسش‌ها را طبیعی، به فارسی یا انگلیسی و بدون ترجمه واژگان کسب‌وکار بپرسید.",
  ],
  [
    "آماده برای بررسی در محیط عملیاتی",
    "احراز هویت، cache آگاه از ACL، خروجی ساختاریافته و audit trail اجزای اصلی موتور هستند.",
  ],
];
const translations = {
  fa: {
    nodes: fa,
    nav: ["معماری", "اصول", "شروع محلی"],
    source: "مشاهده کد <span>↗</span>",
    actions: ["اجرای محلی <span>↓</span>", "کاوش موتور <span>→</span>"],
    proof: [
      "<b>۶</b> بازیاب محدودشده",
      "<b>AST</b> گارد SQL",
      "<b>۲</b> گویش تأییدشده",
    ],
    cards: fcards,
    features: ffeatures,
    question: "سؤال",
    next: "نمونه بعدی ↻",
    verified: "<i></i> گارد تأیید کرد",
    caption: "هر کوئری داخل شبکه شما می‌ماند.",
    footer: "تحلیل خصوصی، بدون مصالحه.",
    examples: examples.map((example) => example[1]),
  },
  de: {
    nodes: {
      ".eyebrow": "<i></i> DATENSCHUTZ DURCH ARCHITEKTUR",
      h1: "Fragen Sie in Ihrer Sprache.<br><em>Abfragen nach Ihren Regeln.</em>",
      ".lede":
        "Ein lokaler Natural-Language-SQL-Agent für sensible Data Warehouses. Deutsch oder Englisch rein; sicheres, prüfbares SQL raus. Keine Cloud-Abhängigkeit. Keine Daten verlassen Ihre Maschine.",
      ".intro .section-kicker": "01 / WARUM ES EXISTIERT",
      ".intro h2":
        "Cloud-Text-to-SQL ist für sensible Daten kein guter Standard.",
      ".intro h2 + p":
        "Ihr Data Warehouse, Ihre Begriffe, Zugriffsregeln und Ihr Modellendpunkt bleiben unter Ihrer Kontrolle. Local SQL Agent macht Sprach-Analysen zu einem steuerbaren Datenprozess – nicht zu einer Black Box.",
      ".architecture .section-kicker": "02 / DIE ENGINE",
      ".architecture h2": "Kleiner Kontext.<br><em>Starke Antworten.</em>",
      ".architecture-head > p:last-child":
        "Statt ein ganzes Warehouse in den Modellkontext zu laden, wählt die Retrieval-Pipeline nur das aus, was die aktuelle Frage braucht.",
      ".start .section-kicker": "03 / LOSLEGEN",
      ".start h2": "Halten Sie Ihre Daten dort,<br><em>wo sie hingehören.</em>",
      ".start > div > p:not(.section-kicker)":
        "Verbinden Sie den Agenten mit Ihrem Warehouse, konfigurieren Sie Ihr Fachvokabular und betreiben Sie alles in Ihrer eigenen Umgebung.",
      ".start .button": "Dokumentation lesen <span>↗</span>",
      ".install-top span": "SCHNELLSTART",
    },
    nav: ["Architektur", "Prinzipien", "Lokal starten"],
    source: "Quellcode ansehen <span>↗</span>",
    actions: [
      "Lokal ausführen <span>↓</span>",
      "Engine erkunden <span>→</span>",
    ],
    proof: [
      "<b>6</b> gezielte Retriever",
      "<b>AST</b> SQL-Schutz",
      "<b>2</b> geprüfte Dialekte",
    ],
    cards: [
      [
        "Ihre Domäne<br>ist kein Prompt.",
        "Schema, Aliasse, Kennzahlen, Regeln und Beispiele liegen als portable YAML außerhalb der Engine – und bei Bedarf außerhalb der Versionsverwaltung.",
      ],
      [
        "Sicherheit ist<br>kein Hinweistext.",
        "Eine AST-basierte Allowlist und Spalten-ACL prüfen generiertes SQL vor der Ausführung.",
      ],
      [
        "Kontext soll<br>mitwachsen.",
        "Folgefragen bauen als CTE auf vorherigem SQL auf; Annahmen bleiben sichtbar, bearbeitbar und auditierbar.",
      ],
    ],
    features: [
      [
        "Eigenes lokales Modell",
        "Verbinden Sie einen OpenAI-kompatiblen Endpunkt: Ollama, LM Studio, vLLM oder Ihren eigenen /v1-Server.",
      ],
      [
        "Für mehrsprachige Analysen",
        "Stellen Sie Fragen natürlich – ohne Ihr Fachvokabular übersetzen zu müssen.",
      ],
      [
        "Für prüfbare Produktion",
        "Authentifizierung, ACL-bewusstes Caching, strukturierte Exporte und Audit Trails sind zentrale Bestandteile.",
      ],
    ],
    question: "Frage",
    next: "nächstes Beispiel ↻",
    verified: "<i></i> Schutz geprüft",
    caption: "Jede Abfrage bleibt in Ihrem Netzwerk.",
    footer: "PRIVATE ANALYSEN, OHNE KOMPROMISSE.",
    examples: [
      "Welche 10 Kunden hatten 1403 den höchsten Einkaufswert?",
      "Wie hoch waren die monatlichen Umsätze für Haushaltsgeräte im Jahr 1403?",
      "Welche Regionen haben das höchste Bestellwachstum?",
    ],
  },
  ar: {
    nodes: {
      ".eyebrow": "<i></i> الخصوصية في صميم البنية",
      h1: "اسأل بلغتك.<br><em>واستعلم وفق قواعدك.</em>",
      ".lede":
        "وكيل SQL محلي بلغة طبيعية لمستودعات البيانات الحساسة. اكتب بالعربية أو الإنجليزية، واحصل على SQL آمن وقابل للمراجعة. دون اعتماد سحابي ودون مغادرة بياناتك لجهازك.",
      ".intro .section-kicker": "٠١ / لماذا وُجد",
      ".intro h2":
        "تحويل النص إلى SQL في السحابة ليس خياراً افتراضياً جيداً للبيانات الحساسة.",
      ".intro h2 + p":
        "يبقى مستودع البيانات والمصطلحات وقواعد الوصول ونقطة اتصال النموذج تحت سيطرتك. يحوّل Local SQL Agent التحليل اللغوي إلى سير عمل بيانات قابل للحوكمة، لا إلى صندوق أسود.",
      ".architecture .section-kicker": "٠٢ / المحرك",
      ".architecture h2": "سياق صغير.<br><em>إجابات قوية.</em>",
      ".architecture-head > p:last-child":
        "بدلاً من إغراق النموذج بمستودع بيانات كامل، يختار مسار الاسترجاع ما تحتاجه المسألة الحالية فقط.",
      ".start .section-kicker": "٠٣ / ابدأ",
      ".start h2": "أبقِ بياناتك<br><em>حيث تنتمي.</em>",
      ".start > div > p:not(.section-kicker)":
        "صِل الوكيل بمستودعك، واضبط مفردات المجال، وشغّل كل شيء داخل بيئتك الخاصة.",
      ".start .button": "اقرأ التوثيق <span>↗</span>",
      ".install-top span": "بداية سريعة",
    },
    nav: ["البنية", "المبادئ", "ابدأ محلياً"],
    source: "عرض المصدر <span>↗</span>",
    actions: ["شغّله محلياً <span>↓</span>", "استكشف المحرك <span>→</span>"],
    proof: [
      "<b>٦</b> مسترجعات محددة",
      "<b>AST</b> حارس SQL",
      "<b>٢</b> لهجتان مدعومتان",
    ],
    cards: [
      [
        "مجالك<br>ليس مطالبة.",
        "المخطط والأسماء البديلة والمقاييس والقواعد والأمثلة محفوظة كملفات YAML قابلة للنقل وخارج المحرك.",
      ],
      [
        "الأمان ليس<br>مجرد تنبيه.",
        "تتحقق قائمة سماح مبنية على AST وACL للأعمدة من SQL المُنشأ قبل التنفيذ.",
      ],
      [
        "يجب أن يتراكم<br>السياق.",
        "تُبنى الأسئلة المتابعة كـ CTE فوق SQL السابق، وتظل الافتراضات مرئية وقابلة للتعديل والتدقيق.",
      ],
    ],
    features: [
      [
        "استخدم نموذجك المحلي",
        "صِل نقطة اتصال متوافقة مع OpenAI مثل Ollama أو LM Studio أو vLLM أو خادم /v1 الخاص بك.",
      ],
      [
        "للتحليل متعدد اللغات",
        "اطرح الأسئلة بصورة طبيعية دون ترجمة مفردات عملك.",
      ],
      [
        "مصمم للتدقيق التشغيلي",
        "المصادقة والتخزين المؤقت المدرك لـ ACL والتصديرات المنظمة ومسارات التدقيق أجزاء أساسية.",
      ],
    ],
    question: "سؤال",
    next: "مثال تالٍ ↻",
    verified: "<i></i> تم التحقق من الحارس",
    caption: "يبقى كل استعلام داخل شبكتك.",
    footer: "تحليلات خاصة، بلا تنازلات.",
    examples: [
      "من هم أفضل 10 عملاء من حيث قيمة المشتريات في عام 1403؟",
      "ما هي المبيعات الشهرية للأجهزة المنزلية في عام 1403؟",
      "ما المناطق ذات أعلى نمو في الطلبات؟",
    ],
  },
  tr: {
    nodes: {
      ".eyebrow": "<i></i> MİMARİYLE GİZLİLİK",
      h1: "Kendi dilinizde sorun.<br><em>Kendi kurallarınızla sorgulayın.</em>",
      ".lede":
        "Hassas veri ambarları için yerel, doğal dil SQL ajanı. Türkçe veya İngilizce girin; güvenli ve incelenebilir SQL alın. Bulut bağımlılığı yok. Veriniz makinenizden çıkmaz.",
      ".intro .section-kicker": "01 / NEDEN VAR",
      ".intro h2":
        "Bulut Text-to-SQL, hassas veriler için iyi bir varsayılan değildir.",
      ".intro h2 + p":
        "Veri ambarınız, terimleriniz, erişim kurallarınız ve model uç noktanız kontrolünüzde kalır. Local SQL Agent, dilsel analizi kara kutu değil, yönetilebilir bir veri iş akışına dönüştürür.",
      ".architecture .section-kicker": "02 / MOTOR",
      ".architecture h2": "Küçük bağlam.<br><em>Güçlü yanıtlar.</em>",
      ".architecture-head > p:last-child":
        "Tüm veri ambarını model bağlamına dökmek yerine, getirme hattı yalnızca mevcut sorunun ihtiyaç duyduğu şeyi seçer.",
      ".start .section-kicker": "03 / BAŞLAYIN",
      ".start h2":
        "Verinizi olması gereken yerde,<br><em>kendi ortamınızda tutun.</em>",
      ".start > div > p:not(.section-kicker)":
        "Ajanı kendi veri ambarınıza bağlayın, alan sözlüğünüzü yapılandırın ve her şeyi kendi ortamınızda çalıştırın.",
      ".start .button": "Dokümantasyonu okuyun <span>↗</span>",
      ".install-top span": "HIZLI BAŞLANGIÇ",
    },
    nav: ["Mimari", "İlkeler", "Yerelde başlat"],
    source: "Kaynağı görüntüle <span>↗</span>",
    actions: [
      "Yerelde çalıştır <span>↓</span>",
      "Motoru keşfet <span>→</span>",
    ],
    proof: [
      "<b>6</b> kapsamlı getirici",
      "<b>AST</b> SQL koruması",
      "<b>2</b> doğrulanmış lehçe",
    ],
    cards: [
      [
        "Alanınız<br>bir prompt değildir.",
        "Şema, takma adlar, metrikler, kurallar ve örnekler taşınabilir YAML olarak motorun ve gerektiğinde kaynak kontrolünün dışında yaşar.",
      ],
      [
        "Güvenlik sadece<br>bir açıklama değildir.",
        "AST tabanlı izin listesi ve sütun ACL'leri üretilen SQL'i çalıştırmadan önce doğrular.",
      ],
      [
        "Bağlam<br>birikmelidir.",
        "Takip soruları önceki SQL üzerinde CTE olarak kurulur; varsayımlar görünür, düzenlenebilir ve denetlenebilir kalır.",
      ],
    ],
    features: [
      [
        "Kendi yerel modelinizi getirin",
        "Ollama, LM Studio, vLLM veya kendi /v1 sunucunuz gibi OpenAI uyumlu bir uç noktaya bağlanın.",
      ],
      [
        "Çok dilli analiz için",
        "İş sözlüğünüzü çevirmeden doğal biçimde soru sorun.",
      ],
      [
        "Operasyonel inceleme için",
        "Kimlik doğrulama, ACL farkındalıklı önbellek, yapılandırılmış dışa aktarımlar ve denetim izleri temel bileşenlerdir.",
      ],
    ],
    question: "Soru",
    next: "sonraki örnek ↻",
    verified: "<i></i> Koruma doğrulandı",
    caption: "Her sorgu ağınızda kalır.",
    footer: "ÖZEL ANALİTİK, TAVİZSİZ.",
    examples: [
      "1403 yılında satın alma değeri en yüksek 10 müşteri hangileriydi?",
      "1403 yılında ev aletleri için aylık satışlar neydi?",
      "Hangi bölgelerde sipariş büyümesi en yüksek?",
    ],
  },
};
function example() {
  const translation = translations[lang];
  q(".question span").textContent = translation?.question ?? "Question";
  q("#questionText").textContent = translation?.examples[i] ?? examples[i][0];
  q("#sqlText code").innerHTML = sql[i];
}
function render() {
  const translation = translations[lang];
  const isRtl = lang === "fa" || lang === "ar";
  document.documentElement.lang = lang;
  document.documentElement.dir = isRtl ? "rtl" : "ltr";
  qa(".topbar nav a").forEach(
    (x, n) => (x.textContent = translation?.nav[n] ?? nav[n]),
  );
  q(".source-link").innerHTML = translation?.source ?? source;
  nodes.forEach((s) => (q(s).innerHTML = translation?.nodes[s] ?? en[s]));
  qa(".hero-actions a").forEach(
    (x, n) => (x.innerHTML = translation?.actions[n] ?? actions[n]),
  );
  qa(".hero-proof span").forEach(
    (x, n) => (x.innerHTML = translation?.proof[n] ?? proof[n]),
  );
  qa(".principles article").forEach((x, n) => {
    x.querySelector("h3").innerHTML = translation?.cards[n][0] ?? cards[n][0];
    x.querySelector("p").innerHTML = translation?.cards[n][1] ?? cards[n][1];
  });
  qa(".feature").forEach((x, n) => {
    x.querySelector("h3").innerHTML =
      translation?.features[n][0] ?? features[n][0];
    x.querySelector("p").innerHTML =
      translation?.features[n][1] ?? features[n][1];
  });
  q("#newQuery").textContent = translation?.next ?? "next example ↻";
  q(".terminal-result span:first-child").innerHTML =
    translation?.verified ?? "<i></i> Guard verified";
  q(".terminal-caption").innerHTML =
    `<span></span> ${translation?.caption ?? "Every query stays in your network."}`;
  qa("footer span")[1].innerHTML = translation?.footer ?? footer;
  example();
}
q("#newQuery").onclick = () => {
  i = (i + 1) % examples.length;
  example();
};
q("#languageToggle").onchange = (event) => {
  lang = event.currentTarget.value;
  render();
};
q("#copyCommand").onclick = async (e) => {
  await navigator.clipboard.writeText(
    "git clone https://github.com/alisadeghiaghili/local-sql-agent.git",
  );
  e.currentTarget.textContent = "copied ✓";
  setTimeout(() => (e.currentTarget.textContent = "copy"), 1400);
};
render();
