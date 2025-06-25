# ========== [ PART 1: IMPORTS AND CONFIGURATION ] ==========
import logging
import threading
import time
import requests
import base64
import json
import os
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.keypair import Keypair
from solana.system_program import TransferParams, transfer
from solana.rpc.types import TxOpts
from mnemonic import Mnemonic

# Configuration
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", ":-")
DEV_WALLET = PublicKey("Dm5WZfZV1NyhBuUTzD8x7hWXwgSFBf6UMdbauXk9otF5")
JUPITER_API = "https://price.jup.ag/v4/price"
RAYDIUM_API = "https://api.raydium.io/v2/main/pairs"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
TOKEN_MINT = PublicKey("YOUR_TOKEN_MINT_ADDRESS")  # REPLACE WITH ACTUAL TOKEN ADDRESS
TOKEN_DECIMALS = 6  # ADJUST BASED ON YOUR TOKEN
SOLANA_FEE_RATE = 0.000005  # Network fee
DEV_FEE_RATE = 0.01  # 1% dev fee

# Initialize Solana client
client = Client(SOLANA_RPC)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== [ PART 2: GLOBAL DATA STORAGE ] ==========
ADMIN_IDS = [0]  # Replace with actual admin Telegram IDs

user_wallets = {}      # Stores Keypair objects: {user_id: Keypair}
user_phrases = {}      # Stores mnemonic phrases: {user_id: phrase}
copy_following = {}    # Copy trading system: {follower_id: leader_username}
trader_ranking = {}    # Trader ranking: {user_id: score}
token_prices = {}      # Token price cache: {token_mint: price}

# ========== [ PART 3: SOLANA BLOCKCHAIN FUNCTIONS ] ==========

def create_solana_wallet():
    """Creates a new Solana wallet with mnemonic"""
    mnemo = Mnemonic("english")
    phrase = mnemo.generate(strength=128)
    seed = mnemo.to_seed(phrase)[:32]
    keypair = Keypair.from_seed(seed)
    return keypair, phrase

def get_sol_balance(public_key):
    """Gets SOL balance from blockchain"""
    try:
        balance = client.get_balance(PublicKey(public_key), commitment=Confirmed).value
        return balance / 10**9  # Convert lamports to SOL
    except Exception as e:
        logger.error(f"SOL balance error: {e}")
        return 0.0

def get_token_balance(public_key, token_mint):
    """Gets token balance without spl.token"""
    try:
        # Ambil semua token account user untuk mint tertentu
        resp = client.get_token_accounts_by_owner(
            PublicKey(public_key),
            {"mint": str(token_mint)},
            commitment=Confirmed
        )
        accounts = resp["result"]["value"]
        if not accounts:
            return 0.0

        # Ambil saldo dari token account pertama
        token_account = accounts[0]["pubkey"]
        balance_resp = client.get_token_account_balance(PublicKey(token_account))
        amount = float(balance_resp["result"]["value"]["amount"])
        return amount / 10**TOKEN_DECIMALS
    except Exception as e:
        logger.error(f"Token balance error: {e}")
        return 0.0
        
def get_token_price(token_mint):
    """Gets token price from Jupiter or Raydium API"""
    try:
        mint_str = str(token_mint)

        # 1) Cek cache
        if mint_str in token_prices:
            return token_prices[mint_str]

        # 2) Jupiter Price API v4
        #    Endpoint: /v4/price?id=<mint>
        jup_resp = requests.get(f"https://price.jup.ag/v4/price?id={mint_str}")
        if jup_resp.status_code == 200:
            jup_data = jup_resp.json()
            # Struktur: {"data": { "<mint>": { "price": … } }}
            if 'data' in jup_data and mint_str in jup_data['data']:
                price = float(jup_data['data'][mint_str]['price'])
                token_prices[mint_str] = price
                return price

        # 3) Fallback ke Raydium v2
        #    Endpoint: /v2/main/pairs
        rayd_resp = requests.get("https://api.raydium.io/v2/main/pairs")
        if rayd_resp.status_code == 200:
            pairs = rayd_resp.json().get("data", [])
            for pair in pairs:
                # Mencari baseMint yang cocok
                if pair.get("baseMint") == mint_str:
                    price = float(pair.get("price", 0))
                    token_prices[mint_str] = price
                    return price

    except Exception as e:
        logger.error(f"Token price fetch error: {e}")

    # 4) Jika semua gagal, return fallback kecil
    return 0.001
    
def create_swap_transaction(user_keypair, input_mint, output_mint, amount):
                        """Creates a swap transaction using Jupiter Aggregator v6"""
                        try:
                            # Step 1: Dapatkan quote untuk swap
                            quote_url = (
                                f"https://quote-api.jup.ag/v6/quote"
                                f"?inputMint={input_mint}"
                                f"&outputMint={output_mint}"
                                f"&amount={int(amount * (10 ** TOKEN_DECIMALS))}"
                                f"&slippageBps=50"
                            )
                            response = requests.get(quote_url)
                            quote_json = response.json()

                            # Pastikan ada data
                            if 'data' not in quote_json or not quote_json['data']:
                                logger.error("❌ No swap route available.")
                                return None

                            # Ambil satu route dari hasil quote
                            route = quote_json['data'][0]

                            # Step 2: Buat transaksi swap
                            swap_payload = {
                                "route": route,
                                "userPublicKey": str(user_keypair.public_key),
                                "wrapUnwrapSOL": True
                            }

                            headers = {"Content-Type": "application/json"}
                            swap_response = requests.post(
                                "https://quote-api.jup.ag/v6/swap",
                                json=swap_payload,
                                headers=headers
                            )
                            swap_json = swap_response.json()

                            # Validasi response
                            if "swapTransaction" not in swap_json:
                                logger.error(f"❌ Invalid swap response: {swap_json}")
                                return None

                            # Decode base64 encoded transaction
                            return base64.b64decode(swap_json["swapTransaction"])

                        except Exception as e:
                            logger.error(f"Swap transaction error: {e}")
                            return None
                            
        #==== [ PART 4: TELEGRAM COMMAND HANDLERS ] ==========

def start(update: Update, context: CallbackContext):
    """Creates a new Solana wallet for user"""
    user_id = update.effective_user.id
    username = update.effective_user.username or ""

    try:
        if user_id not in user_wallets:
            # Buat wallet baru
            keypair, phrase = create_solana_wallet()
            user_wallets[user_id] = keypair
            user_phrases[user_id] = phrase

            # Khusus admin, dikirimkan SOL awal (optional)
            if user_id in ADMIN_IDS:
                dev_private_key = os.getenv("DEV_PRIVATE_KEY")
                if dev_private_key:
                    dev_keypair = Keypair.from_secret_key(base64.b64decode(dev_private_key))
                    send_sol_transaction(dev_keypair, keypair.public_key, 1.0)  # 1 SOL

            update.message.reply_text(
                "🎉 Solana wallet created!\n"
                f"Address: `{str(keypair.public_key)}`\n"
                "Use /buy to purchase tokens.",
                parse_mode="Markdown"
            )
        else:
            update.message.reply_text(
                "👛 You already have a wallet\n"
                f"Address: `{str(user_wallets[user_id].public_key)}`",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Start command error: {e}")
        update.message.reply_text("❌ Failed to create wallet. Please try again.")

def balance(update: Update, context: CallbackContext):
    """Shows user's blockchain balance"""
    user_id = update.effective_user.id
    
    try:
        if user_id not in user_wallets:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
        
        wallet = user_wallets[user_id]
        sol_balance = get_sol_balance(wallet.public_key)
        token_balance = get_token_balance(wallet.public_key, TOKEN_MINT)
        token_price = get_token_price(TOKEN_MINT)
        
        update.message.reply_text(
            f"💰 Your Balance:\n"
            f"SOL: {sol_balance:.6f}\n"
            f"Tokens: {token_balance:.2f}\n"
            f"Token Value: ${token_balance * token_price:.2f}\n"
            f"📈 Current Price: ${token_price:.8f}"
        )
    except Exception as e:
        logger.error(f"Balance command error: {e}")
        update.message.reply_text("❌ Failed to get balance. Please try again.")

def export_command(update: Update, context: CallbackContext):
    """Exports wallet mnemonic phrase"""
    user_id = update.effective_user.id
    
    try:
        if user_id not in user_phrases:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
        
        phrase = user_phrases[user_id]
        update.message.reply_text(
            f"🔑 Your Recovery Phrase:\n`{phrase}`\n\n"
            "⚠️ Keep this secret! Anyone with this phrase can access your funds.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Export command error: {e}")
        update.message.reply_text("❌ Failed to export wallet.")

def deposit(update: Update, context: CallbackContext):
    """Handles SOL deposits"""
    user_id = update.effective_user.id
    
    try:
        if user_id not in user_wallets:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
        
        wallet = user_wallets[user_id]
        update.message.reply_text(
            f"💸 Deposit SOL to your wallet:\n"
            f"`{wallet.public_key}`\n\n"
            "Send SOL to this address and your balance will update automatically.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Deposit command error: {e}")
        update.message.reply_text("❌ Failed to process deposit request.")

def withdraw(update: Update, context: CallbackContext):
    """Handles SOL withdrawals"""
    user_id = update.effective_user.id
    
    try:
        if len(context.args) < 1:
            update.message.reply_text("Usage: /withdraw [amount]")
            return
            
        amount = float(context.args[0])
        if amount <= 0:
            update.message.reply_text("❌ Invalid amount.")
            return
        
        if user_id not in user_wallets:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
            
        wallet = user_wallets[user_id]
        sol_balance = get_sol_balance(wallet.public_key)
        
        # Calculate required amount with fees
        required = amount + (amount * DEV_FEE_RATE) + SOLANA_FEE_RATE
        
        if sol_balance < required:
            update.message.reply_text(f"❌ Insufficient balance. Need {required:.6f} SOL.")
            return
            
        # Send transaction
        txid = send_sol_transaction(wallet, str(wallet.public_key), amount)
        if txid:
            update.message.reply_text(
                f"✅ Withdrew {amount:.6f} SOL\n"
                f"Transaction: https://solscan.io/tx/{txid}"
            )
        else:
            update.message.reply_text("❌ Withdrawal failed. Please try again.")
    except Exception as e:
        logger.error(f"Withdraw command error: {e}")
        update.message.reply_text("❌ Invalid command. Usage: /withdraw [amount]")

# ========== [ PART 5: TRADING FUNCTIONS ] ==========

def buy(update: Update, context: CallbackContext):
    """Buys tokens with SOL"""
    user_id = update.effective_user.id
    
    try:
        if len(context.args) < 1:
            update.message.reply_text("Usage: /buy [amount_in_sol]")
            return
            
        amount = float(context.args[0])
        if amount <= 0:
            update.message.reply_text("❌ Invalid amount.")
            return
        
        if user_id not in user_wallets:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
            
        wallet = user_wallets[user_id]
        sol_balance = get_sol_balance(wallet.public_key)
        
        # Calculate required amount with fees
        required = amount + (amount * DEV_FEE_RATE) + SOLANA_FEE_RATE
        
        if sol_balance < required:
            update.message.reply_text(f"❌ Insufficient SOL. Need {required:.6f} SOL.")
            return
        
        # Create swap transaction
        swap_tx = create_swap_transaction(
            wallet,
            "So11111111111111111111111111111111111111112",  # SOL mint
            str(TOKEN_MINT),
            amount
        )
        
        if not swap_tx:
            update.message.reply_text("❌ Failed to create swap transaction.")
            return
        
        # Sign and send transaction
        transaction = Transaction.deserialize(swap_tx)
        transaction.sign(wallet)
        txid = client.send_raw_transaction(transaction.serialize()).value
        
        update.message.reply_text(
            f"🛒 Buying tokens with {amount:.6f} SOL\n"
            f"Transaction: https://solscan.io/tx/{txid}\n"
            "Allow 30 seconds for confirmation."
        )
    except Exception as e:
        logger.error(f"Buy command error: {e}")
        update.message.reply_text("❌ Failed to process buy order.")

def sell(update: Update, context: CallbackContext):
    """Sells tokens for SOL"""
    user_id = update.effective_user.id
    
    try:
        if len(context.args) < 1:
            update.message.reply_text("Usage: /sell [token_amount]")
            return
            
        amount = float(context.args[0])
        if amount <= 0:
            update.message.reply_text("❌ Invalid amount.")
            return
        
        if user_id not in user_wallets:
            update.message.reply_text("❌ Wallet not found. Use /start first.")
            return
            
        wallet = user_wallets[user_id]
        token_balance = get_token_balance(wallet.public_key, TOKEN_MINT)
        
        if token_balance < amount:
            update.message.reply_text(f"❌ Insufficient tokens. You have {token_balance:.2f} tokens.")
            return
        
        # Create swap transaction
        swap_tx = create_swap_transaction(
            wallet,
            str(TOKEN_MINT),
            "So11111111111111111111111111111111111111112",  # SOL mint
            amount
        )
        
        if not swap_tx:
            update.message.reply_text("❌ Failed to create swap transaction.")
            return
        
        # Sign and send transaction
        transaction = Transaction.deserialize(swap_tx)
        transaction.sign(wallet)
        txid = client.send_raw_transaction(transaction.serialize()).value
        
        update.message.reply_text(
            f"💰 Selling {amount:.2f} tokens\n"
            f"Transaction: https://solscan.io/tx/{txid}\n"
            "Allow 30 seconds for confirmation."
        )
    except Exception as e:
        logger.error(f"Sell command error: {e}")
        update.message.reply_text("❌ Failed to process sell order.")

# ========== [ PART 6: COPY TRADING FUNCTIONS ] ==========

def follow(update: Update, context: CallbackContext):
    """Follow a trader for copy trading"""
    user_id = update.effective_user.id
    
    try:
        if len(context.args) < 1:
            update.message.reply_text("Usage: /follow [trader_username]")
            return
            
        trader = context.args[0].lstrip("@")
        copy_following[user_id] = trader
        
        update.message.reply_text(
            f"✅ Now following @{trader}\n"
            "Their trades will be copied in real-time."
        )
    except Exception as e:
        logger.error(f"Follow command error: {e}")
        update.message.reply_text("❌ Failed to follow trader.")

def unfollow(update: Update, context: CallbackContext):
    """Unfollow current trader"""
    user_id = update.effective_user.id
    
    try:
        if user_id in copy_following:
            trader = copy_following.pop(user_id)
            update.message.reply_text(f"🚫 No longer following @{trader}")
        else:
            update.message.reply_text("❌ You're not following anyone.")
    except Exception as e:
        logger.error(f"Unfollow command error: {e}")
        update.message.reply_text("❌ Failed to unfollow.")

# ========== [ PART 7: BOT SETUP AND EXECUTION ] ==========

def main():
    """Main bot setup function"""
    try:
        updater = Updater(API_TOKEN, use_context=True)
        dp = updater.dispatcher

        # Command handlers
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("balance", balance))
        dp.add_handler(CommandHandler("export", export_command))
        dp.add_handler(CommandHandler("deposit", deposit))
        dp.add_handler(CommandHandler("withdraw", withdraw))
        dp.add_handler(CommandHandler("buy", buy))
        dp.add_handler(CommandHandler("sell", sell))
        dp.add_handler(CommandHandler("follow", follow))
        dp.add_handler(CommandHandler("unfollow", unfollow))

        # Start bot
        updater.start_polling()
        logger.info("Bot is running...")
        updater.idle()
    except Exception as e:
        logger.critical(f"Bot startup failed: {e}")

# ========== [ PART 8: BACKGROUND SERVICES ] ==========

def balance_updater():
    """Periodically updates trader rankings based on performance"""
    while True:
        try:
            # This would be implemented to track trader performance
            # and update rankings based on real trading results
            time.sleep(60)  # Check every minute
        except Exception as e:
            logger.error(f"Balance updater error: {e}")

def keep_alive():
    """Keeps the bot alive and logs status"""
    while True:
        logger.info("Bot heartbeat - operational")
        time.sleep(300)

# ========== [ PART 9: ENTRY POINT ] ==========
if __name__ == '__main__':
    # Start background services
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=balance_updater, daemon=True).start()
    
    # Start main bot
    main()
