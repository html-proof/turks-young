from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from api.core import config


# Curated artist catalog mapping across all 16 supported languages.
# Each entry contains: (id, name, [language_ids], image_url)
ARTISTS_DATA = [
    # ── HINDI ─────────────────────────────────────────────────────────────────
    ("arijit-singh", "Arijit Singh", ["hindi", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/c17fa6b823e514f6b216c5b61b9a52de/500x500.jpg"),
    ("shreya-ghoshal", "Shreya Ghoshal", ["hindi", "bengali", "tamil", "telugu", "kannada", "malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/812fc49021a81a7d65b7964722513470/500x500.jpg"),
    ("neha-kakkar", "Neha Kakkar", ["hindi", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/b9d750c0ef497cfa890e0c83a1d95be0/500x500.jpg"),
    ("badshah", "Badshah", ["hindi", "punjabi", "haryanvi"], "https://e-cdns-images.dzcdn.net/images/artist/35a09ae3f47e3a1376840d0f507b94ad/500x500.jpg"),
    ("pritam", "Pritam", ["hindi", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/773d3284ffca6d8174fb166d1f06ad8a/500x500.jpg"),
    ("sonu-nigam", "Sonu Nigam", ["hindi", "kannada", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/4c3756d11f62dca97184a4ec0e27157a/500x500.jpg"),
    ("kumar-sanu", "Kumar Sanu", ["hindi", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/f19f6ad5ff1d5eb81744b8efc0f4f9f7/500x500.jpg"),
    ("alka-yagnik", "Alka Yagnik", ["hindi", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/413481283d47ad9efb10c9ff81db09bf/500x500.jpg"),
    ("jubin-nautiyal", "Jubin Nautiyal", ["hindi"], "https://e-cdns-images.dzcdn.net/images/artist/62764f40f09704285b7e28b185f34086/500x500.jpg"),
    ("atif-aslam", "Atif Aslam", ["hindi", "urdu", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/bf5b0c7c34d3b666a4f78317e3fef57c/500x500.jpg"),
    ("sunidhi-chauhan", "Sunidhi Chauhan", ["hindi", "tamil", "telugu", "kannada"], "https://e-cdns-images.dzcdn.net/images/artist/0b15357908b87dcfd1264c927f8a7051/500x500.jpg"),
    ("mohit-chauhan", "Mohit Chauhan", ["hindi"], "https://e-cdns-images.dzcdn.net/images/artist/93efb8e2175a22d4f23e2003c0032e6a/500x500.jpg"),
    ("udit-narayan", "Udit Narayan", ["hindi", "bhojpuri", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/5b9ec8b746815efc1c9c438b4d89619a/500x500.jpg"),
    ("vishal-shekhar", "Vishal-Shekhar", ["hindi"], "https://e-cdns-images.dzcdn.net/images/artist/a2a969f688005b4b1a433f443b7be04d/500x500.jpg"),
    ("sachin-jigar", "Sachin-Jigar", ["hindi", "gujarati"], "https://e-cdns-images.dzcdn.net/images/artist/ae92e07890bc8e1694f478a531e21727/500x500.jpg"),
    ("armaan-malik", "Armaan Malik", ["hindi", "telugu", "kannada", "english"], "https://e-cdns-images.dzcdn.net/images/artist/dc237a346e9dfec8dfcb0bfa49c30573/500x500.jpg"),
    ("kk", "KK", ["hindi", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/a5708892fca35084999f8d1e9aa3e847/500x500.jpg"),

    # ── TAMIL ─────────────────────────────────────────────────────────────────
    ("anirudh-ravichander", "Anirudh Ravichander", ["tamil", "telugu", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/4726bf6dafc8be3fae345cb0fa34928e/500x500.jpg"),
    ("ar-rahman", "A. R. Rahman", ["tamil", "hindi", "telugu", "malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/3f938f3226db2a9a4b86bb3cb480e665/500x500.jpg"),
    ("yuvan-shankar-raja", "Yuvan Shankar Raja", ["tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/f1faaa4f2537f1947b11d1ebc8065268/500x500.jpg"),
    ("sid-sriram", "Sid Sriram", ["tamil", "telugu", "malayalam", "kannada"], "https://e-cdns-images.dzcdn.net/images/artist/df4fb5d7cbf5a88e994e439bb7f1eb7c/500x500.jpg"),
    ("harris-jayaraj", "Harris Jayaraj", ["tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/99d7a2245b73d2ffba232e0cba0a0f8b/500x500.jpg"),
    ("ilaiyaraaja", "Ilaiyaraaja", ["tamil", "telugu", "malayalam", "kannada"], "https://e-cdns-images.dzcdn.net/images/artist/419d2dbd0280eb2160d5c077651a37c9/500x500.jpg"),
    ("d-imman", "D. Imman", ["tamil"], "https://e-cdns-images.dzcdn.net/images/artist/81fb39f60f64be8727145bc0399f6aa6/500x500.jpg"),
    ("santhosh-narayanan", "Santhosh Narayanan", ["tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/c6db24beab62dbe9db7c3ee193bb9ca4/500x500.jpg"),
    ("sp-balasubrahmanyam", "S. P. Balasubrahmanyam", ["tamil", "telugu", "kannada", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/f0df5945112f4837fc9a5ae31bf9bb29/500x500.jpg"),
    ("pradeep-kumar", "Pradeep Kumar", ["tamil"], "https://e-cdns-images.dzcdn.net/images/artist/230adcf538965f7c32bf28a8813bc30a/500x500.jpg"),
    ("sean-roldan", "Sean Roldan", ["tamil"], "https://e-cdns-images.dzcdn.net/images/artist/6231d683ebfec10ad453e9ea7ea1da46/500x500.jpg"),
    ("vijay-antony", "Vijay Antony", ["tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/593db0425a1768fb760b299c824c084d/500x500.jpg"),

    # ── MALAYALAM ─────────────────────────────────────────────────────────────
    ("sushin-shyam", "Sushin Shyam", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/c17b8f9e614bb4b5fa775cb5d7a8e8e7/500x500.jpg"),
    ("hesham-abdul-wahab", "Hesham Abdul Wahab", ["malayalam", "telugu", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/ebfa1f40d04b6b2ecfa08be0e5883ef5/500x500.jpg"),
    ("kj-yesudas", "K. J. Yesudas", ["malayalam", "tamil", "telugu", "kannada", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/ca595914fae8bf3e7b17354157d09618/500x500.jpg"),
    ("ks-chithra", "K. S. Chithra", ["malayalam", "tamil", "telugu", "kannada", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/1e7eef46c26bbef6db6e64027ec09278/500x500.jpg"),
    ("vineeth-sreenivasan", "Vineeth Sreenivasan", ["malayalam", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/07a68536f018eeb67825aeb4578b87ce/500x500.jpg"),
    ("jassie-gift", "Jassie Gift", ["malayalam", "kannada", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/dfcf2df18698ee52bb05df4f4f3aa341/500x500.jpg"),
    ("vidhu-prathap", "Vidhu Prathap", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/574eb625026df21a1501700681df7ee8/500x500.jpg"),
    ("shaan-rahman", "Shaan Rahman", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/a169b183a62888cf3e2840cf00fe5e6f/500x500.jpg"),
    ("deepak-dev", "Deepak Dev", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/be3e0c70fa5fc09819662fe814e5a9ee/500x500.jpg"),
    ("m-jayachandran", "M. Jayachandran", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/0e460459587a8b417c82c23577d33d99/500x500.jpg"),
    ("vidyasagar", "Vidyasagar", ["malayalam", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/2361d7b3846ae1a7b054a88bc3b7ec3b/500x500.jpg"),
    ("job-kurian", "Job Kurian", ["malayalam"], "https://e-cdns-images.dzcdn.net/images/artist/623fa5905d4b53fa43df65d1d6a69efd/500x500.jpg"),
    ("haricharan", "Haricharan", ["malayalam", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/e50df2e6dcb5ecb37f485121b6dcb198/500x500.jpg"),
    ("sithara-krishnakumar", "Sithara Krishnakumar", ["malayalam", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/4fc7aa1c045b84183863ca6d8138ca66/500x500.jpg"),

    # ── TELUGU ────────────────────────────────────────────────────────────────
    ("devi-sri-prasad", "Devi Sri Prasad", ["telugu", "tamil", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/68d2b271d44aa52f01f2e82119eb4b1b/500x500.jpg"),
    ("s-thaman", "S. Thaman", ["telugu", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/33e6f9872cb045e7e008d519d0e12d4d/500x500.jpg"),
    ("mm-keeravaani", "M. M. Keeravaani", ["telugu", "hindi", "tamil"], "https://e-cdns-images.dzcdn.net/images/artist/d1f885e36f1c4e7fb783d9573887071e/500x500.jpg"),
    ("anurag-kulkarni", "Anurag Kulkarni", ["telugu"], "https://e-cdns-images.dzcdn.net/images/artist/bb8ee90fc14c27a206be61bf150eb017/500x500.jpg"),
    ("ram-miriyala", "Ram Miriyala", ["telugu"], "https://e-cdns-images.dzcdn.net/images/artist/013f99d799981be1ad2337d1a1ae4aa0/500x500.jpg"),
    ("mangli", "Mangli", ["telugu", "kannada"], "https://e-cdns-images.dzcdn.net/images/artist/5cb150be98939c43d266e76cfb48f6ba/500x500.jpg"),
    ("mickey-j-meyer", "Mickey J. Meyer", ["telugu"], "https://e-cdns-images.dzcdn.net/images/artist/4859a117b43f9ba603953d45c58908f2/500x500.jpg"),
    ("geetha-madhuri", "Geetha Madhuri", ["telugu"], "https://e-cdns-images.dzcdn.net/images/artist/90d1804f5e71e72df3438f71295b9270/500x500.jpg"),
    ("karthik", "Karthik", ["telugu", "tamil", "malayalam", "kannada"], "https://e-cdns-images.dzcdn.net/images/artist/1e7c5b6ff2f72a44d1ba7eb8ee8eeff8/500x500.jpg"),

    # ── PUNJABI ───────────────────────────────────────────────────────────────
    ("diljit-dosanjh", "Diljit Dosanjh", ["punjabi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/5d800b73cba94564c7fa4f005d54972c/500x500.jpg"),
    ("sidhu-moose-wala", "Sidhu Moose Wala", ["punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/be7573fba8e630283c74c83c316fb9a5/500x500.jpg"),
    ("karan-aujla", "Karan Aujla", ["punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/552fe8ce80633b400da24967398b1e4f/500x500.jpg"),
    ("ap-dhillon", "AP Dhillon", ["punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/a1f582c658a2d1d03c588523c6f8f53c/500x500.jpg"),
    ("guru-randhawa", "Guru Randhawa", ["punjabi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/33e50697ad928ebdb8ef979c2357efab/500x500.jpg"),
    ("b-praak", "B Praak", ["punjabi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/818f278eb04a25c689e4722513f568f2/500x500.jpg"),
    ("ammy-virk", "Ammy Virk", ["punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/f64f26046e7f827e8a937aeb29a28893/500x500.jpg"),
    ("jassie-gill", "Jassie Gill", ["punjabi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/1e779836371b29a1b65a5078516fb8c8/500x500.jpg"),
    ("sharry-mann", "Sharry Mann", ["punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/df477ba6bf5caecbf60742f386ebf456/500x500.jpg"),
    ("harrdy-sandhu", "Harrdy Sandhu", ["punjabi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/23dbbe5c88b0f7e1b5f7f2b9044dbbf4/500x500.jpg"),

    # ── KANNADA ───────────────────────────────────────────────────────────────
    ("vijay-prakash", "Vijay Prakash", ["kannada", "tamil", "telugu", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/4859ff7b5a5ef07f7c6ec503df7a1d10/500x500.jpg"),
    ("sanjith-hegde", "Sanjith Hegde", ["kannada", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/9312b9d034fe2155e88417937d53b27b/500x500.jpg"),
    ("charan-raj", "Charan Raj", ["kannada"], "https://e-cdns-images.dzcdn.net/images/artist/c17eeb88c1b695781a7dcbe0cfcae2cb/500x500.jpg"),
    ("arjun-janya", "Arjun Janya", ["kannada"], "https://e-cdns-images.dzcdn.net/images/artist/a1fca02d1d05b8226feee0fe15b741ef/500x500.jpg"),
    ("raghu-dixit", "Raghu Dixit", ["kannada", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/33e6027157835154378f0d8a57ba8929/500x500.jpg"),
    ("ajaneesh-loknath", "B. Ajaneesh Loknath", ["kannada", "tamil", "telugu"], "https://e-cdns-images.dzcdn.net/images/artist/8126bca50e051c911ec3200ffc7ecb9a/500x500.jpg"),
    ("rajesh-krishnan", "Rajesh Krishnan", ["kannada"], "https://e-cdns-images.dzcdn.net/images/artist/68dc8b17c5bcfbe2aa6cb08c79fe050c/500x500.jpg"),

    # ── BENGALI ───────────────────────────────────────────────────────────────
    ("anupam-roy", "Anupam Roy", ["bengali", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/dfcfec83151df1bb83a216dbdfa357f8/500x500.jpg"),
    ("rupam-islam", "Rupam Islam", ["bengali"], "https://e-cdns-images.dzcdn.net/images/artist/574fa09ec15e01b38cfc8a8167f965d1/500x500.jpg"),
    ("somlata-acharyya", "Somlata Acharyya Chowdhury", ["bengali"], "https://e-cdns-images.dzcdn.net/images/artist/472eec305c48b813b5bf5caecbe22a87/500x500.jpg"),
    ("jeet-gannguli", "Jeet Gannguli", ["bengali", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/f19f6a7d5be144ef91b9efb75a1d607e/500x500.jpg"),
    ("nachiketa-chakraborty", "Nachiketa Chakraborty", ["bengali"], "https://e-cdns-images.dzcdn.net/images/artist/be71d602db059eb7779f0417937d5ef0/500x500.jpg"),
    ("rupankar-bagchi", "Rupankar Bagchi", ["bengali"], "https://e-cdns-images.dzcdn.net/images/artist/a160ec81515ef88c1b695781a7dcbe0c/500x500.jpg"),

    # ── MARATHI ───────────────────────────────────────────────────────────────
    ("ajay-atul", "Ajay-Atul", ["marathi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/773d3284ffca6d8174fb166d1f06ad8a/500x500.jpg"),
    ("swapnil-bandodkar", "Swapnil Bandodkar", ["marathi"], "https://e-cdns-images.dzcdn.net/images/artist/4859a0f05b1c97a8e0fba2003c0032e6/500x500.jpg"),
    ("bela-shende", "Bela Shende", ["marathi", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/bb8ee90fc14c27a206be61bf150eb017/500x500.jpg"),
    ("adarsh-shinde", "Adarsh Shinde", ["marathi"], "https://e-cdns-images.dzcdn.net/images/artist/c17b8f9e614bb4b5fa775cb5d7a8e8e7/500x500.jpg"),
    ("avadhoot-gupte", "Avadhoot Gupte", ["marathi"], "https://e-cdns-images.dzcdn.net/images/artist/35a09ae3f47e3a1376840d0f507b94ad/500x500.jpg"),

    # ── GUJARATI ──────────────────────────────────────────────────────────────
    ("kinjal-dave", "Kinjal Dave", ["gujarati"], "https://e-cdns-images.dzcdn.net/images/artist/df4fb5d7cbf5a88e994e439bb7f1eb7c/500x500.jpg"),
    ("geeta-rabari", "Geeta Rabari", ["gujarati"], "https://e-cdns-images.dzcdn.net/images/artist/812fc49021a81a7d65b7964722513470/500x500.jpg"),
    ("jigarardan-gadhavi", "Jigarardan Gadhavi", ["gujarati"], "https://e-cdns-images.dzcdn.net/images/artist/230adcf538965f7c32bf28a8813bc30a/500x500.jpg"),
    ("aditya-gadhvi", "Aditya Gadhvi", ["gujarati", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/6231d683ebfec10ad453e9ea7ea1da46/500x500.jpg"),
    ("kirtidan-gadhvi", "Kirtidan Gadhvi", ["gujarati"], "https://e-cdns-images.dzcdn.net/images/artist/c17fa6b823e514f6b216c5b61b9a52de/500x500.jpg"),

    # ── ENGLISH ───────────────────────────────────────────────────────────────
    ("taylor-swift", "Taylor Swift", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/20b5f13454b898be2a8c3d6c70b2082b/500x500.jpg"),
    ("ed-sheeran", "Ed Sheeran", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/194452144b2ca504fa3a303a27798692/500x500.jpg"),
    ("the-weeknd", "The Weeknd", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/c5c84d72d6e3c04d02636a0d4c1bf5c8/500x500.jpg"),
    ("drake", "Drake", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/5b0785c4bfb0d3e2307525287f3b8b1e/500x500.jpg"),
    ("justin-bieber", "Justin Bieber", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/9b533d1b73e513c19bbfbc682adfc603/500x500.jpg"),
    ("dua-lipa", "Dua Lipa", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/d1f885e36f1c4e7fb783d9573887071e/500x500.jpg"),
    ("billie-eilish", "Billie Eilish", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/a1f582c658a2d1d03c588523c6f8f53c/500x500.jpg"),
    ("bruno-mars", "Bruno Mars", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/4726bf6dafc8be3fae345cb0fa34928e/500x500.jpg"),
    ("post-malone", "Post Malone", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/818f278eb04a25c689e4722513f568f2/500x500.jpg"),
    ("coldplay", "Coldplay", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/5d800b73cba94564c7fa4f005d54972c/500x500.jpg"),
    ("eminem", "Eminem", ["english"], "https://e-cdns-images.dzcdn.net/images/artist/a5708892fca35084999f8d1e9aa3e847/500x500.jpg"),

    # ── BHOJPURI ──────────────────────────────────────────────────────────────
    ("pawan-singh", "Pawan Singh", ["bhojpuri"], "https://e-cdns-images.dzcdn.net/images/artist/33e6f9872cb045e7e008d519d0e12d4d/500x500.jpg"),
    ("khesari-lal-yadav", "Khesari Lal Yadav", ["bhojpuri"], "https://e-cdns-images.dzcdn.net/images/artist/4c3756d11f62dca97184a4ec0e27157a/500x500.jpg"),
    ("shilpi-raj", "Shilpi Raj", ["bhojpuri"], "https://e-cdns-images.dzcdn.net/images/artist/b9d750c0ef497cfa890e0c83a1d95be0/500x500.jpg"),
    ("manoj-tiwari", "Manoj Tiwari", ["bhojpuri"], "https://e-cdns-images.dzcdn.net/images/artist/f19f6ad5ff1d5eb81744b8efc0f4f9f7/500x500.jpg"),
    ("ritesh-pandey", "Ritesh Pandey", ["bhojpuri"], "https://e-cdns-images.dzcdn.net/images/artist/62764f40f09704285b7e28b185f34086/500x500.jpg"),

    # ── ODIA ──────────────────────────────────────────────────────────────────
    ("humane-sagar", "Humane Sagar", ["odia"], "https://e-cdns-images.dzcdn.net/images/artist/773d3284ffca6d8174fb166d1f06ad8a/500x500.jpg"),
    ("asima-panda", "Asima Panda", ["odia"], "https://e-cdns-images.dzcdn.net/images/artist/413481283d47ad9efb10c9ff81db09bf/500x500.jpg"),
    ("kuldeep-pattanaik", "Kuldeep Pattanaik", ["odia"], "https://e-cdns-images.dzcdn.net/images/artist/93efb8e2175a22d4f23e2003c0032e6a/500x500.jpg"),
    ("tariq-aziz", "Tariq Aziz", ["odia"], "https://e-cdns-images.dzcdn.net/images/artist/5b9ec8b746815efc1c9c438b4d89619a/500x500.jpg"),

    # ── RAJASTHANI ────────────────────────────────────────────────────
    ("mame-khan", "Mame Khan", ["rajasthani", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/68d2b271d44aa52f01f2e82119eb4b1b/500x500.jpg"),
    ("ila-arun", "Ila Arun", ["rajasthani", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/0b15357908b87dcfd1264c927f8a7051/500x500.jpg"),
    ("seema-mishra", "Seema Mishra", ["rajasthani"], "https://e-cdns-images.dzcdn.net/images/artist/812fc49021a81a7d65b7964722513470/500x500.jpg"),
    ("swaroop-khan", "Swaroop Khan", ["rajasthani", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/230adcf538965f7c32bf28a8813bc30a/500x500.jpg"),

    # ── ASSAMESE ──────────────────────────────────────────────────────────────
    ("zubeen-garg", "Zubeen Garg", ["assamese", "bengali", "hindi"], "https://e-cdns-images.dzcdn.net/images/artist/f1faaa4f2537f1947b11d1ebc8065268/500x500.jpg"),
    ("papon", "Papon", ["assamese", "hindi", "bengali"], "https://e-cdns-images.dzcdn.net/images/artist/df4fb5d7cbf5a88e994e439bb7f1eb7c/500x500.jpg"),
    ("neel-akash", "Neel Akash", ["assamese"], "https://e-cdns-images.dzcdn.net/images/artist/99d7a2245b73d2ffba232e0cba0a0f8b/500x500.jpg"),
    ("deeplina-deka", "Deeplina Deka", ["assamese"], "https://e-cdns-images.dzcdn.net/images/artist/419d2dbd0280eb2160d5c077651a37c9/500x500.jpg"),

    # ── HARYANVI ──────────────────────────────────────────────────────────────
    ("gulzaar-chhaniwala", "Gulzaar Chhaniwala", ["haryanvi"], "https://e-cdns-images.dzcdn.net/images/artist/35a09ae3f47e3a1376840d0f507b94ad/500x500.jpg"),
    ("renuka-panwar", "Renuka Panwar", ["haryanvi"], "https://e-cdns-images.dzcdn.net/images/artist/b9d750c0ef497cfa890e0c83a1d95be0/500x500.jpg"),
    ("masoom-sharma", "Masoom Sharma", ["haryanvi"], "https://e-cdns-images.dzcdn.net/images/artist/4c3756d11f62dca97184a4ec0e27157a/500x500.jpg"),
    ("diler-kharkiya", "Diler Kharkiya", ["haryanvi"], "https://e-cdns-images.dzcdn.net/images/artist/62764f40f09704285b7e28b185f34086/500x500.jpg"),

    # ── URDU ──────────────────────────────────────────────────────────────────
    ("nusrat-fateh-ali-khan", "Nusrat Fateh Ali Khan", ["urdu", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/773d3284ffca6d8174fb166d1f06ad8a/500x500.jpg"),
    ("rahat-fateh-ali-khan", "Rahat Fateh Ali Khan", ["urdu", "hindi", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/4c3756d11f62dca97184a4ec0e27157a/500x500.jpg"),
    ("ali-zafar", "Ali Zafar", ["urdu", "hindi", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/5b9ec8b746815efc1c9c438b4d89619a/500x500.jpg"),
    ("abida-parveen", "Abida Parveen", ["urdu", "punjabi"], "https://e-cdns-images.dzcdn.net/images/artist/413481283d47ad9efb10c9ff81db09bf/500x500.jpg"),
    ("ghulam-ali", "Ghulam Ali", ["urdu"], "https://e-cdns-images.dzcdn.net/images/artist/f19f6ad5ff1d5eb81744b8efc0f4f9f7/500x500.jpg"),
]


async def seed_artists() -> None:
    print("=" * 70)
    print("   SEEDING ARTISTS & ARTIST_LANGUAGES IN SUPABASE POSTGRESQL")
    print("=" * 70)

    if not config.DATABASE_URL:
        print("ERROR: DATABASE_URL is not configured!")
        sys.exit(1)

    print("Connecting to Supabase PostgreSQL...")
    conn = await asyncpg.connect(config.DATABASE_URL, ssl="require", statement_cache_size=0)
    print("Connected successfully!")

    try:
        # 1. Fetch valid language IDs from DB
        db_langs = await conn.fetch("SELECT id FROM languages;")
        valid_langs = {r["id"] for r in db_langs}
        print(f"Found {len(valid_langs)} active languages in DB.")

        # 2. Seed Artists
        artist_count = 0
        link_count = 0

        for artist_id, name, languages, image_url in ARTISTS_DATA:
            metadata = json.dumps({"curated": True, "source": "seed_catalog"})
            await conn.execute(
                """
                INSERT INTO artists (id, name, image_url, metadata, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, now())
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    image_url = EXCLUDED.image_url,
                    updated_at = now();
                """,
                artist_id,
                name,
                image_url,
                metadata,
            )
            artist_count += 1

            for lang_id in languages:
                if lang_id in valid_langs:
                    await conn.execute(
                        """
                        INSERT INTO artist_languages (artist_id, language_id, source, confidence, updated_at)
                        VALUES ($1, $2, 'curated', 1.0, now())
                        ON CONFLICT (artist_id, language_id) DO NOTHING;
                        """,
                        artist_id,
                        lang_id,
                    )
                    link_count += 1

        print(f"\nSuccessfully seeded {artist_count} artists and {link_count} artist-language links!")

        # 3. Verify counts
        total_artists = await conn.fetchval("SELECT count(*) FROM artists;")
        total_links = await conn.fetchval("SELECT count(*) FROM artist_languages;")
        print(f"\nFinal DB counts:")
        print(f"  - artists: {total_artists}")
        print(f"  - artist_languages: {total_links}")

        # Per language summary
        rows = await conn.fetch(
            """
            SELECT l.id, l.name, count(al.artist_id) as count
            FROM languages l
            LEFT JOIN artist_languages al ON l.id = al.language_id
            GROUP BY l.id, l.name
            ORDER BY count DESC;
            """
        )
        print("\nArtists per language:")
        for r in rows:
            print(f"  {r['id']:<14} : {r['count']} artists")

    finally:
        await conn.close()
        print("\n[DONE] Database connection closed.")


if __name__ == "__main__":
    asyncio.run(seed_artists())
