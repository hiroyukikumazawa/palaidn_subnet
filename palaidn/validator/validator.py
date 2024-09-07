from argparse import ArgumentParser
import bittensor as bt
import torch
import json
from typing import List, Dict, Any, Tuple
import sqlite3
import os
import sys
from copy import deepcopy
import copy
from datetime import datetime, timedelta, timezone
from palaidn.protocol import PalaidnData
import uuid
from pathlib import Path
from os import path, rename
import requests
import time
from dotenv import load_dotenv
import os
import asyncio
import concurrent.futures

from palaidn.utils.fraud_data import FraudData

# Get the current file's directory
current_dir = os.path.dirname(os.path.abspath(__file__))

# Get the parent directory
parent_dir = os.path.dirname(current_dir)

# Get the grandparent directory
grandparent_dir = os.path.dirname(parent_dir)

# Get the great grandparent directory
great_grandparent_dir = os.path.dirname(grandparent_dir)

# Add parent, grandparent, and great grandparent directories to sys.path
sys.path.append(parent_dir)
sys.path.append(grandparent_dir)
sys.path.append(great_grandparent_dir)
from base.neuron import BaseNeuron
from dotenv import load_dotenv


class PalaidnValidator(BaseNeuron):

    alchemy_api_key = 'empty'

    default_db_path = "data/fraud.db"

    fraud_data = FraudData()

    alchemy_transactions = None

    def __init__(self, parser: ArgumentParser):
        args = parser.parse_args()

        super().__init__(parser=parser, profile="validator")
        parser.add_argument(
            "--db",
            type=str,
            default=self.default_db_path,
            help="Path to the validator database",
        )

        # Check if the arguments are already defined before adding them
        if not any(arg.dest == 'subtensor.network' for arg in parser._actions):
            parser.add_argument('--subtensor.network', type=str, help="The subtensor network to connect to")
        if not any(arg.dest == 'subtensor.chain_endpoint' for arg in parser._actions):
            parser.add_argument('--subtensor.chain_endpoint', type=str, help="The subtensor chain_endpoint to connect to")
        if not any(arg.dest == 'netuid' for arg in parser._actions):
            parser.add_argument('--netuid', type=int, help="The network UID")
        if not any(arg.dest == 'wallet.name' for arg in parser._actions):
            parser.add_argument('--wallet.name', type=str, help="The name of the wallet to use")
        if not any(arg.dest == 'wallet.hotkey' for arg in parser._actions):
            parser.add_argument('--wallet.hotkey', type=str, help="The hotkey of the wallet to use")
        if not any(arg.dest == 'logging.debug' for arg in parser._actions):
            parser.add_argument('--logging.debug', action='store_true', help="Enable debug logging")

        args = parser.parse_args()

        bt.logging.add_args(parser)
        bt.logging.debug("Parsed arguments2:", args)

        self.timeout = 12
        self.neuron_config = None
        self.wallet =  None
        self.subtensor = None
        self.dendrite = None
        self.metagraph = None
        self.scores = None
        self.hotkeys = None
        self.subtensor_connection = None
        self.miner_responses = None
        self.max_targets = None
        self.target_group = None
        self.blacklisted_miner_hotkeys = []
        self.load_validator_state = None
        self.data_entry = None
        self.uid = None
        self.loop = asyncio.get_event_loop()
        self.thread_executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix='asyncio')
        self.axon_port = getattr(args, 'axon.port', None) 

        load_dotenv()  # take environment variables from .env.
        self.paypangea_api_key = os.getenv('PAYPANGEA_API_KEY')

    def apply_config(self, bt_classes) -> bool:
        """applies the configuration to specified bittensor classes"""
        try:
            self.neuron_config = self.config(bt_classes=bt_classes)
        except AttributeError as e:
            bt.logging.error(f"unable to apply validator configuration: {e}")
            raise AttributeError from e
        except OSError as e:
            bt.logging.error(f"unable to create logging directory: {e}")
            raise OSError from e

        return True

    async def initialize_connection(self):
        if self.subtensor is None:
            try:
                self.subtensor = bt.subtensor(config=self.neuron_config)
                bt.logging.info(f"Connected to {self.neuron_config.subtensor.network} network")
            except Exception as e:
                bt.logging.error(f"Failed to initialize subtensor: {str(e)}")
                self.subtensor = None

        return self.subtensor

    async def get_subtensor(self):
        if self.subtensor is None:
            self.subtensor = bt.subtensor(config=self.neuron_config)
        return self.subtensor

    async def sync_metagraph(self):
        subtensor = await self.get_subtensor()
        self.metagraph.sync(subtensor=subtensor, lite=True)
        return self.metagraph

    def check_vali_reg(self, metagraph, wallet, subtensor) -> bool:
        """validates the validator has registered correctly"""
        if wallet.hotkey.ss58_address not in metagraph.hotkeys:
            bt.logging.error(
                f"your validator: {wallet} is not registered to chain connection: {subtensor}. run btcli register and try again"
            )
            return False

        return True

    def setup_bittensor_objects(
        self, neuron_config
    ) -> Tuple[bt.wallet, bt.subtensor, bt.dendrite, bt.metagraph]:
        """sets up the bittensor objects"""
        try:
            wallet = bt.wallet(config=neuron_config)
            subtensor = bt.subtensor(config=neuron_config)
            dendrite = bt.dendrite(wallet=wallet)
            metagraph = subtensor.metagraph(neuron_config.netuid)
        except AttributeError as e:
            bt.logging.error(f"unable to setup bittensor objects: {e}")
            raise AttributeError from e

        self.hotkeys = copy.deepcopy(metagraph.hotkeys)

        return wallet, subtensor, dendrite, metagraph

    def serve_axon(self):
        """Serve the axon to the network"""
        bt.logging.info("Serving axon...")
        
        self.axon = bt.axon(wallet=self.wallet)

        self.axon.serve(netuid=self.neuron_config.netuid, subtensor=self.subtensor)

    def initialize_neuron(self) -> bool:
        """initializes the neuron

        Args:
            none

        Returns:
            bool:
                a boolean value indicating success/failure of the initialization
        Raises:
            AttributeError:
                AttributeError is raised if the neuron initialization failed
            IndexError:
                IndexError is raised if the hotkey cannot be found from the metagraph
        """

        bt.logging.set_config(config=self.neuron_config.logging)

        bt.logging.debug(
            f"initializing validator for subnet: {self.neuron_config.netuid} on network: {self.neuron_config.subtensor.chain_endpoint} with config: {self.neuron_config}"
        )

        # setup the bittensor objects
        wallet, subtensor, dendrite, metagraph = self.setup_bittensor_objects(
            self.neuron_config
        )

        bt.logging.info(
            f"bittensor objects initialized:\nmetagraph: {metagraph}\nsubtensor: {subtensor}\nwallet: {wallet}\nlogging: {dendrite}"
        )
 
        # validate that the validator has registered to the metagraph correctly
        if not self.validator_validation(metagraph, wallet, subtensor):
            raise IndexError("unable to find validator key from metagraph")

        # get the unique identity (uid) from the network
        validator_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)

        self.uid = validator_uid
        bt.logging.info(f"validator is running with uid: {validator_uid}")

        self.wallet = wallet
        self.subtensor = subtensor
        self.dendrite = dendrite
        self.metagraph = metagraph

        # read command line arguments and perform actions based on them
        args = self._parse_args(parser=self.parser)

        if args:
            if args.load_state == "False":
                self.load_validator_state = False
            else:
                self.load_validator_state = True

            if self.load_validator_state:
                self.load_state()
            else:
                self.init_default_scores()

            if args.max_targets:
                self.max_targets = args.max_targets
            else:
                self.max_targets = 256
            self.db_path = args.db
        else:
            # setup initial scoring weights
            self.init_default_scores()
            self.max_targets = 256
            self.db_path = self.default_db_path

        self.target_group = 0

        return True

    def _parse_args(self, parser):
        """parses the command line arguments"""
        return parser.parse_args()

    def check_erc20_transaction_exists(self, transaction_hash, base_address, sender):
        """
        Checks if a transaction exists on Ethereum using Alchemy API and returns two bools:
        [transaction_exists, had_error]
        """

        # First check if base_address matches sender
        if base_address != sender:
            bt.logging.error(f"Sender {sender} does not match base_address {base_address}.")
            return [False, False]  # Transaction doesn't exist, no error occurred

        url = f"https://eth-mainnet.g.alchemy.com/v2/{self.alchemy_api_key}"

        headers = {
            "accept": "application/json",
            'Content-Type': 'application/json',
        }

        # Prepare JSON-RPC payload to query transaction by hash
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionByHash",
            "params": [transaction_hash],
            "id": 1,
        }


        bt.logging.debug(f"Calling alchemy for check_erc20_transaction_exists.")

        try:
            # Make the API request to Alchemy
            response = requests.post(url, json=payload, headers=headers)
            response_data = response.json()

            # Check if the transaction exists
            if response_data.get("result") is not None:

                result_query = response_data.get("result")

                # Extract 'from' and 'to' fields from the result
                transaction_from = result_query.get("from")
                transaction_to = result_query.get("to")

                # Check if the base_address exists in 'from' or 'to' fields
                if base_address not in [transaction_from, transaction_to]:
                    bt.logging.error(f"Base address {base_address} is not present in 'from' or 'to' fields.")

                    return [False, False]  # Transaction doesn't exist, no error
                        

                return [True, False]  # Transaction exists, no error
            else:
                return [False, False]  # Transaction does not exist, no error

        except Exception as e:
            bt.logging.error(f"Error querying Alchemy for transaction {transaction_hash}: {e}")
            return [False, True]  # Transaction doesn't exist, but there was an error
  
    def validator_validation(self, metagraph, wallet, subtensor) -> bool:
        """this method validates the validator has registered correctly"""
        if wallet.hotkey.ss58_address not in metagraph.hotkeys:
            bt.logging.error(
                f"your validator: {wallet} is not registered to chain connection: {subtensor}. run btcli register and try again"
            )
            return False

        return True

    def connect_db(self):
        """connects to the sqlite database"""
        return sqlite3.connect(self.db_path)
        
    def process_miner_data(
            self, processed_uids: torch.tensor, transactions: list
        ) -> list:
            """
            Processes responses received by miners.

            Args:
                processed_uids: list of uids that have been processed
                transactions: list of deserialized synapses
            """

            transactions_dict = {}

            bt.logging.warning(f"Processing data sent from UID {self.uid}.")

            # Initialize a dictionary to track how many miners fetched each transaction
            transaction_counter = {}

            # Store transactions that need to be checked (fetched by <80% miners)
            transactions_to_check = []

            total_synapses = len(transactions)  # Total number of synapses

            # Define a threshold of 80%
            threshold = 0.8 

            # # Iterate over synapse transactions
            # for synapse in transactions:
            #     # Ensure synapse has expected elements
            #     if synapse.wallet_address:
            #         transaction_data = synapse.transactions_dict
            #         base_address = synapse.wallet_address
            #         uid = synapse.neuron_uid

            #         if self.hotkeys[uid] not in self.blacklisted_miner_hotkeys:
            #             if uid == self.uid:
            #                 bt.logging.debug(f"{uid} is offline or is not a miner")
            #             else:
            #                 # Ensure transaction_data is not None and not empty before processing
            #                 if transaction_data is not None and transaction_data != []:
            #                     bt.logging.debug(
            #                         f"Miner {uid} fetched transactions and they will be checked: {len(transaction_data)}"
            #                     )

            #                     # Count how many miners fetched each transaction
            #                     for txn in transaction_data:
            #                         txn_id = txn.transaction_hash
            #                         if txn_id:
            #                             transaction_counter[txn_id] = transaction_counter.get(txn_id, 0) + 1

            #                     # After counting, filter transactions to be checked (fetched by <80% of miners)
            #                     filtered_transactions = [
            #                         txn for txn in transaction_data
            #                         if transaction_counter[txn.transaction_hash] < threshold or transaction_counter[txn.transaction_hash] < 5
            #                     ]

            #                     # Log and insert filtered transactions
            #                     if filtered_transactions:
            #                         bt.logging.debug(
            #                             f"Miner {uid} provided {len(filtered_transactions)} transactions that will be saved for further checking."
            #                         )

            #                         # Store the uid, base_address, and filtered_transactions in transactions_to_check
            #                         transactions_to_check.append({
            #                             "uid": uid,
            #                             "hotkey": self.hotkeys[uid],
            #                             "base_address": base_address,
            #                             "filtered_transactions": filtered_transactions
            #                         })

            #                     else:
            #                         bt.logging.debug(f"All transactions from miner {uid} were fetched by >= 80% of miners, skipping.")

                                
            #                 else:
            #                     bt.logging.debug(f"UID {uid} responded, but did not fetch any transactions and will be skipped.")
            #         else:
            #             bt.logging.warning("Miner was blacklisted, I do not care what he sends :)")
            #     else:
            #         bt.logging.warning("Synapse data is incomplete or not in the expected format.")

            # First pass: count how many miners fetched each transaction
            for synapse in transactions:
                if synapse.wallet_address:
                    transaction_data = synapse.transactions_dict
                    uid = synapse.neuron_uid

                    # Check if miner's hotkey is not blacklisted
                    if self.hotkeys[uid] not in self.blacklisted_miner_hotkeys:
                        if transaction_data:
                            # Count how many miners fetched each transaction
                            for txn in transaction_data:
                                txn_id = txn.transaction_hash
                                if txn_id:
                                    transaction_counter[txn_id] = transaction_counter.get(txn_id, 0) + 1

            # Second pass: filter transactions for further checking
            for synapse in transactions:
                if synapse.wallet_address:
                    transaction_data = synapse.transactions_dict
                    base_address = synapse.wallet_address
                    uid = synapse.neuron_uid

                    # Check if miner's hotkey is not blacklisted
                    if self.hotkeys[uid] not in self.blacklisted_miner_hotkeys:
                        if transaction_data:
                            # Filter transactions that need further checking
                            filtered_transactions = [
                                txn for txn in transaction_data
                                if transaction_counter[txn.transaction_hash] < threshold * total_synapses or total_synapses < 5
                            ]

                            # Log and store filtered transactions
                            if filtered_transactions:
                                bt.logging.debug(
                                    f"Miner {uid} provided {len(filtered_transactions)} transactions that will be saved for further checking."
                                )

                                transactions_to_check.append({
                                    "uid": uid,
                                    "hotkey": self.hotkeys[uid],
                                    "base_address": base_address,
                                    "filtered_transactions": filtered_transactions
                                })
                            else:
                                bt.logging.debug(f"All transactions from miner {uid} were fetched by >= 80% of miners, skipping.")
                else:
                    bt.logging.warning("Synapse data is incomplete or not in the expected format.")


            # Process the transactions_to_check array
            for txn_info in transactions_to_check:
                uid = txn_info["uid"]
                hotkey = txn_info["hotkey"]
                base_address = txn_info["base_address"]
                filtered_transactions = txn_info["filtered_transactions"]

                # Process each filtered transaction
                for txn in filtered_transactions:
                    transaction_hash = txn.transaction_hash

                    # Only perform the blockchain check and blacklisting if the UID is not blacklisted
                    if hotkey not in self.blacklisted_miner_hotkeys:
                        if transaction_hash:
                            # Check if the category is "erc20"
                            if txn.category == "erc20":
                                # Call existing function for ERC20
                                existsAndValid = self.check_alchemy_transaction(transaction_hash, base_address, txn.sender)
                            else:
                                # For any other category, use the new method with alchemy_transactions
                                existsAndValid = self.check_alchemy_transaction(transaction_hash, base_address, txn.sender)

                            # First value: whether the transaction exists and is valid
                            # Second value: whether an error occurred
                            if existsAndValid[0]:  # Transaction exists and is valid
                                bt.logging.debug(f"Transaction {transaction_hash} exists on the blockchain and is valid.")
                            else:
                                # Handle the error case if there was one
                                if existsAndValid[1]:  # An error occurred
                                    bt.logging.error(f"Error occurred while checking transaction {transaction_hash} for miner {uid}.")
                                else:
                                    # Transaction does not exist or is invalid, no error during the check
                                    bt.logging.warning(f"Transaction {transaction_hash} does not exist on the blockchain, miner {uid} made it up.")
                                    # Blacklist the miner who made up the transaction
                                    self.blacklist_miner(hotkey)

                    else:
                        bt.logging.debug(f"Skipping blockchain check for blacklisted UID {uid}.")

            # Iterate over synapse transactions and save to DB if valid
            for synapse in transactions:
                # Ensure synapse has expected elements
                if synapse.wallet_address:
                    transaction_data = synapse.transactions_dict
                    uid = synapse.neuron_uid

                    if self.hotkeys[uid] not in self.blacklisted_miner_hotkeys:
                        if uid == self.uid:
                            bt.logging.debug(f"{uid} is offline or is not a miner")
                        else:
                            # Ensure transaction_data is not None and not empty before processing
                            if transaction_data is not None and transaction_data != []:
                                bt.logging.debug(
                                    f"Miner {uid} fetched transactions and they will be saved: {len(transaction_data)}"
                                )
                                
                                # Insert all transactions into the database
                                self.fraud_data.insert_into_database(base_address, transaction_data, self.metagraph.hotkeys)
                            else:
                                bt.logging.debug(f"UID {uid} responded, but did not fetch any transactions and will be skipped.")
                    else:
                        bt.logging.warning("Miner was blacklisted, I do not care what he sends :)")
                else:
                    bt.logging.warning("Synapse data is incomplete or not in the expected format.")

    def add_new_miners(self):
        """
        adds new miners to the database, if there are new hotkeys in the metagraph
        """
        if self.hotkeys:
            uids_with_stake = self.metagraph.total_stake >= 0.0
            for i, hotkey in enumerate(self.metagraph.hotkeys):
                if (hotkey not in self.hotkeys) and (i not in uids_with_stake):
                    coldkey = self.metagraph.coldkeys[i]

                    if self.miner_stats.init_miner_row(hotkey, coldkey, i):
                        bt.logging.info(f"added new miner to the database: {hotkey}")
                    else:
                        bt.logging.error(
                            f"failed to add new miner to the database: {hotkey}"
                        )

    def blacklist_miner(self, uid):
        """Add the miner UID to the blacklist."""
        
        # Initialize as an empty set if it's None
        if self.blacklisted_miner_hotkeys is None:
            self.blacklisted_miner_hotkeys = set()

        # Add the hotkey to the blacklist if it's not already present
        if uid not in self.blacklisted_miner_hotkeys:
            self.blacklisted_miner_hotkeys.append(uid)

            self.save_state()
            bt.logging.info(f"Miner {uid} has been blacklisted.")


    def check_hotkeys(self):
        """Checks if some hotkeys have been replaced or removed in the metagraph and adjusts scores accordingly."""
            # Ensure blacklisted_miner_hotkeys is initialized as an empty list if it's None
        if self.blacklisted_miner_hotkeys is None:
            self.blacklisted_miner_hotkeys = []

        if self.hotkeys:
            # Check if the known state length matches with the current metagraph hotkey length
            current_hotkeys = self.metagraph.hotkeys

            if len(self.hotkeys) == len(current_hotkeys):
                for i, hotkey in enumerate(current_hotkeys):
                    # Check for mismatching hotkeys and reset scores
                    if self.hotkeys[i] != hotkey:
                        bt.logging.debug(
                            f"index '{i}' has mismatching hotkey. old hotkey: '{self.hotkeys[i]}', new hotkey: '{hotkey}'. Resetting score to 0.0"
                        )
                        bt.logging.debug(f"Score before reset: {self.scores[i]}")
                        self.scores[i] = 0.0
                        bt.logging.debug(f"Score after reset: {self.scores[i]}")

                # Remove replaced hotkeys from the blacklist
                hotkeys_to_remove = []
                for blacklisted_hotkey in self.blacklisted_miner_hotkeys:
                    if blacklisted_hotkey not in current_hotkeys:
                        bt.logging.info(f"Removing replaced hotkey '{blacklisted_hotkey}' from blacklist.")
                        hotkeys_to_remove.append(blacklisted_hotkey)
                
                # Remove the hotkeys that have been replaced
                for hotkey in hotkeys_to_remove:
                    self.blacklisted_miner_hotkeys.remove(hotkey)

            elif len(self.hotkeys) < len(current_hotkeys):
                # If the metagraph has more hotkeys, append 0.0 for new ones
                bt.logging.info(
                    f"Metagraph has more hotkeys, adjusting scores. "
                    f"Expected: {len(current_hotkeys)}, had: {len(self.hotkeys)}"
                )
                while len(self.scores) < len(current_hotkeys):
                    self.scores.append(0.0)
                    bt.logging.debug(f"Added 0.0 for new hotkey, total scores: {len(self.scores)}")

            else:
                # If the metagraph has fewer hotkeys, initialize default scores
                bt.logging.info(
                    f"Metagraph has fewer hotkeys, initializing default scores. "
                    f"Expected: {len(current_hotkeys)}, had: {len(self.hotkeys)}"
                )
                self.init_default_scores()

            # Update the local copy of hotkeys to match the metagraph
            self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        else:
            # Initialize hotkeys and scores for the first time
            self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)
            self.scores = [0.0] * len(self.hotkeys)
            bt.logging.info(f"Initialized scores for {len(self.hotkeys)} hotkeys.")

    def init_default_scores(self) -> None:
        """Validators without previous validation knowledge should start
        with default score of 0.0 for each uid. The method can also be
        used to reset the scores in case of an internal error."""
        
        bt.logging.info("initiating validator with default scores for each uid")
        self.scores = [0.0] * len(self.metagraph.uids)
        bt.logging.info(f"validation weights have been initialized: {self.scores}")

    def save_state(self):
        """saves the state of the validator to a file"""
        bt.logging.info("saving validator state")

        # save the state of the validator to file
        torch.save(
            {
                "step": self.step,
                "scores": self.scores,
                "hotkeys": self.hotkeys,
                "last_updated_block": self.last_updated_block,
                "blacklisted_miner_hotkeys": self.blacklisted_miner_hotkeys,
            },
            self.base_path + "/state.pt",
        )

        bt.logging.debug(
            f"saved the following state to a file: step: {self.step}, scores: {self.scores}, hotkeys: {self.hotkeys}, last_updated_block: {self.last_updated_block}, blacklisted_miner_hotkeys: {self.blacklisted_miner_hotkeys}"
        )

    def reset_validator_state(self, state_path):
        """inits the default validator state. should be invoked only
        when an exception occurs and the state needs to reset"""

        # rename current state file in case manual recovery is needed
        rename(
            state_path,
            f"{state_path}-{int(datetime.now().timestamp())}.autorecovery",
        )

        self.init_default_scores()
        self.step = 0
        self.last_updated_block = 0
        self.hotkeys = None
        self.blacklisted_miner_hotkeys = None

    def load_state(self):
        """loads the state of the validator from a file"""

        # load the state of the validator from file
        state_path = self.base_path + "/state.pt"
        if path.exists(state_path):
            try:
                bt.logging.info("loading validator state")
                state = torch.load(state_path)
                bt.logging.debug(f"loaded the following state from file: {state}")
                self.step = state["step"]
                self.scores = state["scores"]
                self.hotkeys = state["hotkeys"]
                self.last_updated_block = state["last_updated_block"]
                # if "blacklisted_miner_hotkeys" in state.keys():
                #     self.blacklisted_miner_hotkeys = state["blacklisted_miner_hotkeys"]

                bt.logging.info(f"scores loaded from saved file: {self.scores}")
            except Exception as e:
                bt.logging.error(
                    f"validator state reset because an exception occurred: {e}"
                )
                self.reset_validator_state(state_path=state_path)

        else:
            self.init_default_scores()

    def get_uids_to_query(self, all_axons) -> list:
        """returns the list of uids to query"""

        # get uids with a positive stake
        uids_with_stake = self.metagraph.total_stake >= 0.0
        bt.logging.trace(f"uids with a positive stake: {uids_with_stake}: {len(uids_with_stake)}")

        # get uids with an ip address of 0.0.0.0
        invalid_uids = torch.tensor(
            [
                bool(value)
                for value in [
                    ip != "0.0.0.0"
                    for ip in [
                        self.metagraph.neurons[uid].axon_info.ip
                        for uid in self.metagraph.uids.tolist()
                    ]
                ]
            ],
            dtype=torch.bool,
        )
        bt.logging.trace(f"uids with 0.0.0.0 as an ip address: {invalid_uids}")

        # get uids that have their hotkey blacklisted
        blacklisted_uids = []
        if self.blacklisted_miner_hotkeys:
            for hotkey in self.blacklisted_miner_hotkeys:
                if hotkey in self.metagraph.hotkeys:
                    blacklisted_uids.append(self.metagraph.hotkeys.index(hotkey))
                else:
                    bt.logging.trace(
                        f"blacklisted hotkey {hotkey} was not found from metagraph"
                    )

            bt.logging.debug(f"blacklisted the following uids: {blacklisted_uids}")

        # convert blacklisted uids to tensor
        blacklisted_uids_tensor = torch.tensor(
            [uid not in blacklisted_uids for uid in self.metagraph.uids.tolist()],
            dtype=torch.bool,
        )

        bt.logging.trace(f"blacklisted uids: {blacklisted_uids_tensor}")

        # determine the uids to filter
        uids_to_filter = torch.logical_not(
            ~blacklisted_uids_tensor | ~invalid_uids | ~uids_with_stake
        )

        bt.logging.trace(f"uids to filter: {uids_to_filter}")

        # define uids to query
        uids_to_query = [
            axon
            for axon, keep_flag in zip(all_axons, uids_to_filter)
            if keep_flag.item()
        ]

        # define uids to filter
        final_axons_to_filter = [
            axon
            for axon, keep_flag in zip(all_axons, uids_to_filter)
            if not keep_flag.item()
        ]

        uids_not_to_query = [
            self.metagraph.hotkeys.index(axon.hotkey) for axon in final_axons_to_filter
        ]

        bt.logging.trace(f"final axons to filter: {final_axons_to_filter}")
        bt.logging.debug(f"filtered uids: {uids_not_to_query}")

        # reduce the number of simultaneous uids to query
        if self.max_targets < 256:
            start_idx = self.max_targets * self.target_group
            end_idx = min(
                len(uids_to_query), self.max_targets * (self.target_group + 1)
            )
            if start_idx == end_idx:
                return [], []
            if start_idx >= len(uids_to_query):
                raise IndexError(
                    "starting index for querying the miners is out-of-bounds"
                )

            if end_idx >= len(uids_to_query):
                end_idx = len(uids_to_query)
                self.target_group = 0
            else:
                self.target_group += 1

            bt.logging.debug(
                f"list indices for uids to query starting from: '{start_idx}' ending with: '{end_idx}'"
            )
            uids_to_query = uids_to_query[start_idx:end_idx]

        list_of_uids = [
            self.metagraph.hotkeys.index(axon.hotkey) for axon in uids_to_query
        ]

        list_of_hotkeys = [axon.hotkey for axon in uids_to_query]

        bt.logging.debug(f"sending query to the following hotkeys: {list_of_hotkeys}: {len(list_of_hotkeys)}")

        return uids_to_query, list_of_uids, blacklisted_uids, uids_not_to_query

    async def run_sync_in_async(self, fn):
        return await self.loop.run_in_executor(self.thread_executor, fn)

    def calculate_miner_scores(self):
        """
        Calculates the scores for miners based on their performance in the last 12 hours.
        The score is the number of transactions they submitted. All times are in UTC.
        If the miner is blacklisted, their transaction count is set to 0.
        """
        # Initialize earnings to 1.0 for each miner (1.0 is the base score)
        earnings = [1.0] * len(self.metagraph.uids)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        now = datetime.now(timezone.utc)
        timeframe = now - timedelta(hours=12)

        # Query the transaction count for each miner in the last 12 hours
        cursor.execute(
            """
            SELECT minerID, COUNT(*) as transaction_count
            FROM wallet_transactions
            WHERE scan_date >= ? AND scan_date <= ?
            GROUP BY minerID
            """,
            (timeframe.isoformat(), now.isoformat())
        )
        transaction_rows = cursor.fetchall()

        conn.close()

        # Process the transaction counts and adjust earnings for each miner
        for row in transaction_rows:
            miner_id, transaction_count = row

            int_miner_id = int(miner_id)  # Convert minerID to an integer

            # If the miner is blacklisted, set their transaction count to 0
            if self.metagraph.hotkeys[int_miner_id] in self.blacklisted_miner_hotkeys:
                bt.logging.debug(f"Miner {int_miner_id} is blacklisted. Transaction count set to 0.")
                transaction_count = 0
            else:
                bt.logging.debug(f"Miner {int_miner_id} has {transaction_count} transactions.")

            # Add the transaction count to the miner's score
            earnings[int_miner_id] += transaction_count

            bt.logging.debug(f"{int_miner_id}: miner_performance {earnings[int_miner_id]}")

        bt.logging.debug("Miner performance calculated")
        bt.logging.debug(f"Scans {earnings}")
        return earnings

    async def set_weights(self):
        bt.logging.info("Entering set_weights method")
        # Calculate miner scores and normalize weights as before
        earnings = self.calculate_miner_scores()
        
        total_earnings = sum(earnings)

        # Normalize the array
        if total_earnings > 0:
            weights = [e / total_earnings for e in earnings]
        else:
            weights = earnings  # If total is 0, keep the original earnings

        bt.logging.debug(f"earnings: {weights}")
        # Check stake
        uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        stake = float(self.metagraph.S[uid])
        if stake < 1.0:
            bt.logging.error("Insufficient stake. Failed in setting weights.")
            return False

        if self.subtensor is None:
            bt.logging.warning("Subtensor is None. Attempting to reinitialize...")
            self.subtensor = await self.initialize_connection()
            if self.subtensor is None:
                bt.logging.error("Failed to reinitialize subtensor. Cannot set weights.")
                return False
            
            # self.subtensor.blocks_since_last_update(self.neuron_config.netuid, self.uid) > self.subtensor.weights_rate_limit(self.neuron_config.netuid)


        try:
            # Check if enough blocks have passed since the last update
            if self.subtensor.blocks_since_last_update(self.neuron_config.netuid, self.uid) > self.subtensor.weights_rate_limit(self.neuron_config.netuid):
                bt.logging.info("Attempting to set weights with 120 second timeout")
                
                # Run set_weights in a separate thread using asyncio.to_thread
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.subtensor.set_weights,
                        netuid=self.neuron_config.netuid,
                        wallet=self.wallet,
                        uids=self.metagraph.uids,
                        weights=weights,
                        version_key=self.spec_version,
                        wait_for_inclusion=False,
                        wait_for_finalization=True,
                        max_retries=3
                    ),
                    timeout=120  # 120 second timeout
                )

                # Process the result
                if result[0] is True:
                    bt.logging.debug(f"Set weights result: {result}")
                    return True
                else:
                    bt.logging.warning(f"set_weights failed {result}")

            else:
                # If not enough blocks have passed, calculate the blocks to wait
                blocks_since_last_update = self.subtensor.blocks_since_last_update(self.neuron_config.netuid, self.uid)
                weights_rate_limit = self.subtensor.weights_rate_limit(self.neuron_config.netuid)
                blocks_to_wait = weights_rate_limit - blocks_since_last_update
                bt.logging.info(f"Need to wait {blocks_to_wait} more blocks to set weight.")

        except asyncio.TimeoutError:
            bt.logging.error("Timeout occurred while setting weights (120 seconds elapsed).")
        except Exception as e:
            bt.logging.error(f"Error setting weight: {str(e)}")


        
        return False

    def check_alchemy_transaction(self, transaction_hash, base_address, sender):
        """
        Checks if a non-ERC20 transaction exists in alchemy_transactions. 
        If alchemy_transactions is empty, fetch new transactions from Alchemy.
        """
        if not self.alchemy_transactions:
            try:
                # Reset alchemy_transactions and populate it with the latest transfers with a timeout
                self.alchemy_transactions = self.get_erc20_transfers(base_address, timeout=10)  # Set a 10-second timeout
            except requests.Timeout:
                bt.logging.error(f"Timeout occurred while fetching ERC20 transfers for {base_address}.")
                return [False, True]  # Error occurred
            except requests.RequestException as e:
                bt.logging.error(f"Error fetching ERC20 transfers for {base_address}: {e}")
                return [False, True]  # General error occurred
        
        # Search for the transaction in the alchemy_transactions list
        for txn in self.alchemy_transactions:
            if txn['hash'] == transaction_hash:
                bt.logging.debug(f"Transaction {transaction_hash} found in alchemy transactions.")
                return [True, False]  # Transaction exists, no error

        # Transaction not found
        bt.logging.debug(f"Transaction {transaction_hash} not found in alchemy transactions.")
        return [False, False]  # Transaction does not exist, no error
    
    def get_erc20_transfers(self, wallet_address: str, timeout: int = 10) -> List[Dict[str, Any]]:
        """
        Retrieves ERC20, ERC721, and ERC1155 transfers for a wallet address from Alchemy.
        Includes a timeout to prevent hanging.
        """
        url = f"https://eth-mainnet.g.alchemy.com/v2/{self.alchemy_api_key}"

        headers = {
            'Content-Type': 'application/json'
        }

        payload = {
            "jsonrpc": "2.0",
            "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": "0x0",
                "toBlock": "latest",
                "fromAddress": wallet_address,
                "category": ["erc721", "erc20", "erc1155"],
                "withMetadata": True,
                "excludeZeroValue": True
            }],
            "id": 1
        }


        bt.logging.debug(f"Calling alchemy for transactions.")

        try:
            # Make the request with a timeout to avoid hanging
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()  # Raise exception for bad responses (4xx/5xx)

            data = response.json()

            return data.get('result', {}).get('transfers', [])

        except requests.Timeout:
            bt.logging.error(f"Timeout occurred while fetching transfers for {wallet_address}.")
            return []  # Return an empty list if the request times out

        except requests.RequestException as e:
            bt.logging.error(f"Error fetching transfers for {wallet_address}: {e}")
            return []  # Return an empty list in case of any other error