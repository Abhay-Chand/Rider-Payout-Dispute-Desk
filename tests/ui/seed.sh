m(){ curl -s -o /dev/null -w "%{http_code} " localhost:8000/messages -H 'content-type: application/json' -d "{\"message_id\":\"$1\",\"rider_id\":\"$2\",\"text\":\"$3\",\"received_at\":\"$4\"}"; }
m w1 R016 "19 sept ke 5 orders ka paisa hi nahi aaya" 2026-09-22T11:30:00+05:30
m w2 R030 "20 ko penalty do baar kata aur 19 ko order T404692 ka surge nahi mila" 2026-09-23T10:00:00+05:30
m w3 R018 "mujhe manager se baat karni hai" 2026-09-22T13:20:00+05:30
m w4 R003 "Bhai order T926334 ka surge nahi mila, 20 tarikh wala" 2026-09-22T09:05:00+05:30
m w5 R036 "18 ko order T990831 ka distance galat laga hai" 2026-09-23T12:00:00+05:30
echo
