TRENDCART  -  Backend + Database (Python Flask + SQLite)
=========================================================

KAISE CHALAYE (sirf pehli baar setup)
1. Python install karo: https://www.python.org/downloads/  ->  "Download Python" button.
   !! Installer ki PEHLI screen pe neeche "Add python.exe to PATH" ka box TICK karna zaruri hai !!
   Phir "Install Now".
2. Is poore folder ko extract karo (zip pe right click -> Extract All).
3. start.bat pe DOUBLE CLICK karo.
   - Pehli baar 1-2 minute lagenge (Flask apne aap install hoga).
   - Browser khud khul jayega: http://localhost:3000
4. Band karne ke liye black window close kar do.

   Mac/Linux: terminal me  ./start.sh

DEMO LOGIN:  admin  /  1234
(Real use se pehle admin ka password badal dena / account hata dena.)

IMPORTANT: static/index.html ko seedha double-click karke mat kholo.
Hamesha start.bat chalao aur http://localhost:3000 se kholo, tabhi backend kaam karega.

KYA KYA SAVE HOTA HAI (database file: trendcart.db, pehli baar chalne par apne aap ban jati hai)
- users               : signup ke accounts (password hashed save hota hai, plain text nahi)
- products            : store ke saare products
- cart_items          : har user ka cart (refresh/logout ke baad bhi bacha rehta hai)
- addresses          : customer ke delivery addresses (next time automatically dikhte hai)
- orders, order_items : checkout complete hone par bane orders (address, payment method, delivery charge sab ke saath)
- contact_messages    : Contact form ke messages
- subscribers         : Newsletter ke emails

CHECKOUT FLOW (Meesho jaisa)
- Product kholo -> "Buy now"  : Address -> Order Summary -> Payment -> Order Confirmation
- Cart -> "Proceed to checkout": Cart -> Address -> Order Summary -> Payment -> Order Confirmation
- Payment options: UPI, Card, Cash on Delivery.
  !! Abhi koi real payment gateway nahi hai (demo). UPI/Card order turant "Paid" mark hota hai, COD "Pending".
  Card ka sirf last 4 digit save hota hai. Real payment ke liye Razorpay/PhonePe jaisa gateway app.py ke
  place_order() me jodna padega.
- Delivery charge / free delivery / delivery days badalne ke liye app.py ke upar ye 3 lines badlo:
  DELIVERY_FEE = 49, FREE_DELIVERY_ABOVE = 999, ETA_DAYS = 5
- Purana trendcart.db ho to bhi chalega, naye columns apne aap add ho jate hai.

SIZES (products table)
- sizes     : product ki saari sizes, comma se alag, jaise  S,M,L,XL  ya  UK 7,UK 8,UK 9
              (khali chhodo to size nahi lagegi, jaise bag/watch/cap ke liye).
- sizes_out : jo sizes abhi stock me nahi hai, jaise  XXL  (customer ko cut-out dikhti hai, order nahi hota).
- Customer ko size chunni padti hai; cart aur order me size save hoti hai (same product ki alag size = alag item).
- Demo ke liye kuch products me ek size out of stock rakhi hai (jacket XXL, sneaker UK 6, hoodie S). DB Browser me badal sakte ho.
- Size ke naam me ' | " ya , mat likho.

BILL (INVOICE) + PDF DOWNLOAD
- Order place hone ke baad confirmation page pe "See your bill" button aata hai. My Orders me bhi har order pe "See your bill" hai.
- Bill me: bill number, order date, delivery address, product + size + qty + price, subtotal, delivery charge, total, payment status.
- "Download bill (PDF)" se PDF download hoti hai (reportlab library + fonts/ folder use hota hai - fonts folder delete mat karna).
- Pehle se purana setup ho to bhi start.bat naya reportlab apne aap install kar dega.

MY ORDERS + TRACKING
- Upar header me box wale icon ya "My Orders" link se khulta hai (order confirm hone ke baad "Track this order" se bhi).
- Stages: Order placed -> Packed -> Shipped -> Out for delivery -> Delivered (ya Cancelled).
- app.py me AUTO_TRACKING = True hai: order din ke hisaab se khud aage badhta hai
  (1 din baad Packed, 2 din baad Shipped, delivery se 1 din pehle Out for delivery, delivery date par Delivered).
- Khud control karna ho: AUTO_TRACKING = False karo, phir DB Browser me orders table ka "status" badlo:
  placed / packed / shipped / out_for_delivery / delivered / cancelled
- Customer order tab tak cancel kar sakta hai jab tak wo Shipped nahi hua. Online payment wale order me "Refund initiated"
  dikhta hai (real refund ke liye payment gateway jodna padega).

DATABASE KAISE DEKHE / PRODUCTS KAISE BADLE
1. "DB Browser for SQLite" install karo (free): https://sqlitebrowser.org
2. Usme trendcart.db kholo (Open Database).
3. "Browse Data" tab me table choose karo (products, orders, users...).
4. Product add/edit karna ho to products table me row badlo, phir "Write Changes" (Ctrl+S).
   Columns: name, cat (category), price (rupees),
            icon (jacket / sneaker / bag / tee / cap / watch / sunglasses / hoodie),
            tag (featured = top section, new = new arrivals, shop = sirf shop page),
            desc (description), fill + bg (colour, jaise #6B7A4B), img (photo ka link, optional).
   Site refresh karne par naya data dikh jayega.

FILES
- app.py              : backend (API + database) - sab kuch isi ek file me
- products-seed.json  : pehli baar ke 12 products
- static/index.html   : poori website (frontend)
- requirements.txt    : Python libraries ki list (sirf Flask)
- trendcart.db        : DATABASE (isko backup rakho; delete kiya to sab data reset ho jayega)

ONLINE HOST KARNA HO
PythonAnywhere / Render jaisi service pe Flask app ki tarah deploy karo. SQLite file save rakhne ke
liye persistent storage chahiye, aur environment variable SECRET_KEY set karna.
