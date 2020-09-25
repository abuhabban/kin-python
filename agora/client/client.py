import base64
from typing import List, Optional

import grpc
import kin_base

from agora import KIN_2_PROD_NETWORK, KIN_2_TEST_NETWORK
from agora.client.environment import Environment
from agora.client.internal import V3InternalClient, SubmitTransactionResult
from agora.error import AccountExistsError, InvoiceError, InvoiceErrorReason, \
    UnsupportedVersionError, TransactionMalformedError, SenderDoesNotExistError, InsufficientBalanceError, \
    DestinationDoesNotExistError, InsufficientFeeError, BadNonceError, \
    TransactionRejectedError, Error, AlreadyPaidError, \
    WrongDestinationError, SkuNotFoundError
from agora.model.earn import Earn
from agora.model.invoice import InvoiceList
from agora.keys import PrivateKey, PublicKey
from agora.model.memo import AgoraMemo
from agora.model.payment import Payment
from agora.model.result import BatchEarnResult, EarnResult
from agora.model.transaction import TransactionData
from agora.model.transaction_type import TransactionType
from agora.retry import retry, LimitStrategy, BackoffWithJitterStrategy, BinaryExponentialBackoff, \
    NonRetriableErrorsStrategy, RetriableErrorsStrategy
from agora.utils import partition, quarks_to_kin

_SUPPORTED_VERSIONS = [2, 3]

_ENDPOINTS = {
    Environment.PRODUCTION: 'api.agorainfra.net:443',
    Environment.TEST: 'api.agorainfra.dev:443',
}

# kin_base handles conversion of the network name to the appropriate passphrase if recognizes it, but otherwise will
# use the provided network name as the passphrase
_NETWORK_NAMES = {
    2: {
        Environment.PRODUCTION: KIN_2_PROD_NETWORK,
        Environment.TEST: KIN_2_TEST_NETWORK,
    },
    3: {
        Environment.PRODUCTION: 'PUBLIC',
        Environment.TEST: 'TESTNET',
    },
}

_KIN_2_ISSUERS = {
    Environment.PRODUCTION: 'GDF42M3IPERQCBLWFEZKQRK77JQ65SCKTU3CW36HZVCX7XX5A5QXZIVK',
    Environment.TEST: 'GBC3SG6NGTSZ2OMH3FFGB7UVRQWILW367U4GSOOF4TFSZONV42UJXUH7',
}

_KIN_2_ASSET_CODE = 'KIN'

_NON_RETRIABLE_ERRORS = [
    AccountExistsError,
    TransactionMalformedError,
    SenderDoesNotExistError,
    DestinationDoesNotExistError,
    InsufficientBalanceError,
    InsufficientFeeError,
    TransactionRejectedError,
    InvoiceError,
    BadNonceError,
]

_GRPC_TIMEOUT_SECONDS = 10


class RetryConfig:
    """A :class:`RetryConfig <RetryConfig>` for configuring retries for Agora requests.

    :param max_retries: (optional) The max number of times the client will retry a request, excluding the initial
        attempt. Defaults to 5 if value is not provided or value is below 0.
    :param max_nonce_refreshes: (optional) The max number of times the client will attempt to refresh a nonce, excluding
        the initial attempt. Defaults to 3 if value is not provided or value is below 0.
    :param min_delay: (optional) The minimum amount of time to delay between request retries, in seconds. Defaults to
        0.5 seconds if value is not provided or value is below 0.
    :param min_delay: (optional) The maximum amount of time to delay between request retries, in seconds. Defaults to
        5 seconds if value is not provided or value is below 0.
    """

    def __init__(
        self, max_retries: Optional[int] = None, min_delay: Optional[float] = None, max_delay: Optional[float] = None,
        max_nonce_refreshes: Optional[int] = None,
    ):
        self.max_retries = max_retries if max_retries is not None and max_retries >= 0 else 5
        self.min_delay = min_delay if min_delay is not None and min_delay >= 0 else 0.5
        self.max_delay = max_delay if max_delay is not None and max_delay >= 0 else 10
        self.max_nonce_refreshes = (max_nonce_refreshes if max_nonce_refreshes is not None and max_nonce_refreshes >= 0
                                    else 3)


class BaseClient:
    """An interface for accessing Agora features.
    """

    def create_account(self, private_key: PrivateKey):
        """Creates a new Kin account.

        :param private_key: The :class:`PrivateKey <agora.model.keys.PrivateKey>` of the account to create
        :raise: :exc:`UnsupportedVersionError <agora.error.UnsupportedVersionError>`
        :raise: :exc:`AccountExistsError <agora.error.AccountExistsError>`
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement create_account')

    def get_transaction(self, tx_hash: bytes) -> TransactionData:
        """Retrieves a transaction.

        :param tx_hash: The hash of the transaction to retrieve
        :return: a :class:`TransactionData <agora.model.transaction.TransactionData>` object.
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement get_transaction')

    def get_balance(self, public_key: PublicKey) -> int:
        """Retrieves the balance of an account.

        :param public_key: The :class:`PublicKey <agora.model.keys.PublicKey>` of the account to retrieve the balance for.
        :raise: :exc:`UnsupportedVersionError <agora.error.UnsupportedVersionError>`
        :raise: :exc:`AccountNotFoundError <agora.error.AccountNotFoundError>`
        :return: The balance of the account, in quarks.
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement get_balance')

    def submit_payment(self, payment: Payment) -> bytes:
        """Submits a payment to the Kin blockchain.

        :param payment: The :class:`Payment <agora.model.payment.Payment>` to submit.

        :raise: :exc:`UnsupportedVersionError <agora.error.UnsupportedVersionError>`
        :raise: :exc:`TransactionMalformedError <agora.error.TransactionMalformedError>`
        :raise: :exc:`InvalidSignatureError <agora.error.InvalidSignatureError>`
        :raise: :exc:`InsufficientBalanceError <agora.error.InsufficientBalanceError>`
        :raise: :exc:`InsufficientFeeError <agora.error.InsufficientFeeError>`
        :raise: :exc:`SenderDoesNotExistError <agora.error.SenderDoesNotExistError>`
        :raise: :exc:`DestinationDoesNotExistError <agora.error.DestinationDoesNotExistError>`
        :raise: :exc:`BadNonceError <agora.error.BadNonceError>`
        :raise: :exc:`TransactionError <agora.error.TransactionError>`
        :raise: :exc:`InvoiceError <agora.error.InvoiceError>`

        :return: The hash of the transaction.
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement submit_payment')

    def submit_earn_batch(
        self, sender: PrivateKey, earns: List[Earn], channel: Optional[PrivateKey] = None, memo: Optional[str] = None
    ) -> BatchEarnResult:
        """Submit multiple earn payments.

        :param sender: The :class:`PrivateKey <agora.model.keys.PrivateKey>` of the sender
        :param earns: A list of :class:`Earn <agora.model.earn.Earn>` objects.
        :param channel: (optional) The :class:`PrivateKey <agora.model.keys.PrivateKey>` of a channel account to use as
            the transaction source. If not set, the `sender` will be used as the source.
        :param memo: (optional) The memo to include in the transaction. If set, none of the invoices included in earns
            will be applied.

        :raise: :exc:`UnsupportedVersionError <agora.error.UnsupportedVersionError>`

        :return: a :class:`BatchEarnResult <agora.model.result.BatchEarnResult>`
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement submit_earn_batch')

    def close(self) -> None:
        """Closes the connection-related resources (e.g. the gRPC channel) used by the client. Subsequent requests to
        this client will cause an exception to be thrown.
        """
        raise NotImplementedError('BaseClient is an abstract class. Subclasses must implement close')


class Client(BaseClient):
    """A :class:`Client <Client>` object for accessing Agora API features.

    :param env: The :class:`Environment <agora.environment.Environment>` to use.
    :param app_index: (optional) The Agora index of the app, used for all transactions and requests. Required to make
        use of invoices.
    :param whitelist_key: (optional) The :class:`PrivateKey <agora.model.keys.PrivateKey>` of the account to whitelist
        submitted transactions with.
    :param grpc_channel: (optional) A GRPC :class:`Channel <grpc.Channel>` object to use for Agora requests. Only one of
        grpc_channel or endpoint should be set.
    :param endpoint: (optional) An endpoint to use instead of the default Agora endpoints. Only one of grpc_channel or
        endpoint should be set.
    :param retry_config: (optional): A :class:`RetryConfig <RetryConfig>` object to configure Agora retries. If not
        provided, a default configuration will be used.
    :param kin_version: (optional): The version of Kin to use. Defaults to using Kin 3.
    """

    def __init__(
        self, env: Environment, app_index: int = 0, whitelist_key: Optional[PrivateKey] = None,
        grpc_channel: Optional[grpc.Channel] = None, endpoint: Optional[str] = None,
        retry_config: Optional[RetryConfig] = None, kin_version: Optional[int] = 3,
    ):
        if kin_version not in _SUPPORTED_VERSIONS:
            raise ValueError(f'{kin_version} is not a supported version of Kin')

        self.network_name = _NETWORK_NAMES[kin_version][env]
        self.app_index = app_index
        self.whitelist_key = whitelist_key

        if grpc_channel and endpoint:
            raise ValueError('`grpc_channel` and `endpoint` cannot both be set')

        if not grpc_channel:
            endpoint = endpoint if endpoint else _ENDPOINTS[env]
            ssl_credentials = grpc.ssl_channel_credentials()
            self._grpc_channel = grpc.secure_channel(endpoint, ssl_credentials)
        else:
            self._grpc_channel = grpc_channel

        retry_config = retry_config if retry_config else RetryConfig()
        internal_retry_strategies = [
            NonRetriableErrorsStrategy(_NON_RETRIABLE_ERRORS),
            LimitStrategy(retry_config.max_retries + 1),
            BackoffWithJitterStrategy(BinaryExponentialBackoff(retry_config.min_delay),
                                      retry_config.max_delay, 0.1),
        ]
        self._nonce_retry_strategies = [
            RetriableErrorsStrategy([BadNonceError]),
            LimitStrategy(retry_config.max_nonce_refreshes + 1)
        ]

        self._kin_version = kin_version
        if kin_version == 2:
            self._asset_issuer = _KIN_2_ISSUERS[env]
        else:
            self._asset_issuer = None

        self._internal_client = V3InternalClient(self._grpc_channel, internal_retry_strategies, self._kin_version)

    def create_account(self, private_key: PrivateKey):
        if self._kin_version not in _SUPPORTED_VERSIONS:
            raise UnsupportedVersionError()

        self._internal_client.create_account(private_key)

    def get_transaction(self, tx_hash: bytes) -> TransactionData:
        return self._internal_client.get_transaction(tx_hash)

    def get_balance(self, public_key: PublicKey) -> int:
        if self._kin_version not in _SUPPORTED_VERSIONS:
            raise UnsupportedVersionError()

        info = self._internal_client.get_account_info(public_key)
        return info.balance

    def submit_payment(self, payment: Payment) -> bytes:
        if self._kin_version not in _SUPPORTED_VERSIONS:
            raise UnsupportedVersionError()

        if payment.invoice and self.app_index <= 0:
            raise ValueError("cannot submit a payment with an invoice without an app index")

        builder = self._get_stellar_builder(payment.channel if payment.channel else payment.sender)

        invoice_list = None
        if payment.memo:
            builder.add_text_memo(payment.memo)
        elif self.app_index > 0:
            if payment.invoice:
                invoice_list = InvoiceList(invoices=[payment.invoice])

            fk = invoice_list.get_sha_224_hash() if payment.invoice else b''
            memo = AgoraMemo.new(1, payment.tx_type, self.app_index, fk)
            builder.add_hash_memo(memo.val)

        # Inside the kin_base module, the base currency has been 'scaled' by a factor of 100 from
        # Stellar (i.e., the smallest denomination used is 1e-5 instead of 1e-7). However, Kin 2 uses the minimum
        # Stellar denomination of 1e-7.
        #
        # The Kin amounts provided to `append_payment_op` get converted to the smallest denomination inside the
        # submitted transaction and the conversion occurs assuming a smallest denomination of 1e-5. Therefore, for
        # Kin 2 transactions, we must multiple by 100 to account for the scaling factor.
        builder.append_payment_op(
            payment.destination.stellar_address,
            quarks_to_kin(payment.quarks * 100 if self._kin_version == 2 else payment.quarks),
            source=payment.sender.public_key.stellar_address,
            asset_issuer=self._asset_issuer if self._kin_version == 2 else None,
        )

        if payment.channel:
            signers = [payment.channel, payment.sender]
        else:
            signers = [payment.sender]

        if self.whitelist_key:
            signers.append(self.whitelist_key)

        result = self._sign_and_submit_builder(signers, builder, invoice_list)
        if result.tx_error:
            if len(result.tx_error.op_errors) > 0:
                if len(result.tx_error.op_errors) != 1:
                    raise Error(f'invalid number of operation errors, expected 0 or 1, got '
                                f'{len(result.tx_error.op_errors)}')
                raise result.tx_error.op_errors[0]

            if result.tx_error.tx_error:
                raise result.tx_error.tx_error

        if result.invoice_errors:
            if len(result.invoice_errors) != 1:
                raise Error(f'invalid number of invoice errors, expected 0 or 1, got {len(result.invoice_errors)}')

            if result.invoice_errors[0].reason == InvoiceErrorReason.ALREADY_PAID:
                raise AlreadyPaidError()
            if result.invoice_errors[0].reason == InvoiceErrorReason.WRONG_DESTINATION:
                raise WrongDestinationError()
            if result.invoice_errors[0].reason == InvoiceErrorReason.SKU_NOT_FOUND:
                raise SkuNotFoundError()
            raise Error(f'unknown invoice error: {result.invoice_errors[0].reason}')

        return result.tx_hash

    def submit_earn_batch(
        self, sender: PrivateKey, earns: List[Earn], channel: Optional[bytes] = None, memo: Optional[str] = None
    ) -> BatchEarnResult:
        if self._kin_version not in _SUPPORTED_VERSIONS:
            raise UnsupportedVersionError

        invoices = [earn.invoice for earn in earns if earn.invoice]
        if invoices:
            if self.app_index <= 0:
                raise ValueError('cannot submit a payment with an invoice without an app index')
            if len(invoices) != len(earns):
                raise ValueError('Either all or none of the earns must contain invoices')
            if memo:
                raise ValueError('Cannot use both text memo and invoices')

        succeeded = []
        failed = []
        for earn_batch in partition(earns, 100):
            try:
                result = self._submit_earn_batch_tx(sender, earn_batch, channel, memo)
            except Error as e:
                failed += [EarnResult(earn, error=e) for idx, earn in enumerate(earn_batch)]
                break

            if not result.tx_error:
                succeeded += [EarnResult(earn, tx_hash=result.tx_hash) for earn in earn_batch]
                continue

            # At this point, the batch is considered failed.
            err = result.tx_error

            if err.op_errors:
                failed += [EarnResult(earn, tx_hash=result.tx_hash, error=err.op_errors[idx])
                           for idx, earn in enumerate(earn_batch)]
            else:
                failed += [EarnResult(earn, tx_hash=result.tx_hash, error=err.tx_error)
                           for idx, earn in enumerate(earn_batch)]
            break

        for earn in earns[len(succeeded) + len(failed):]:
            failed.append(EarnResult(earn=earn))

        return BatchEarnResult(succeeded=succeeded, failed=failed)

    def close(self) -> None:
        self._grpc_channel.close()

    def _submit_earn_batch_tx(
        self, sender: PrivateKey, earns: List[Earn], channel: Optional[PrivateKey] = None, memo: Optional[str] = None
    ) -> SubmitTransactionResult:
        """ Submits a single transaction for a batch of earns. An error will be raised if the number of earns exceeds
        the capacity of a single transaction.

        :param sender: The :class:`PrivateKey <agora.model.keys.PrivateKey>` of the sender
        :param earns: A list of :class:`Earn <agora.model.earn.Earn>` objects.
        :param channel: (optional) The :class:`PrivateKey <agora.model.keys.PrivateKey>` of the channel account to use
            as the transaction source. If not set, the sender will be used as the source.
        :param memo: (optional) The memo to include in the transaction. If set, none of the invoices included in earns
            will be applied.

        :return: a list of :class:`BatchEarnResult <agora.model.result.EarnResult>` objects
        """
        if len(earns) > 100:
            raise ValueError('cannot send more than 100 earns')

        builder = self._get_stellar_builder(channel if channel else sender)

        invoices = [earn.invoice for earn in earns if earn.invoice]
        invoice_list = InvoiceList(invoices) if invoices else None
        if memo:
            builder.add_text_memo(memo)
        elif self.app_index > 0:
            fk = invoice_list.get_sha_224_hash() if invoice_list else b''
            memo = AgoraMemo.new(1, TransactionType.EARN, self.app_index, fk)
            builder.add_hash_memo(memo.val)

        for earn in earns:
            # Inside the kin_base module, the base currency has been 'scaled' by a factor of 100 from
            # Stellar (i.e., the smallest denomination used is 1e-5 instead of 1e-7). However, Kin 2 uses the minimum
            # Stellar denomination of 1e-7.
            #
            # The Kin amounts provided to `append_payment_op` get converted to the smallest denomination inside the
            # submitted transaction and the conversion occurs assuming a smallest denomination of 1e-5. Therefore, for
            # Kin 2 transactions, we must multiple by 100 to account for the scaling factor.
            builder.append_payment_op(
                earn.destination.stellar_address,
                quarks_to_kin(earn.quarks * 100 if self._kin_version == 2 else earn.quarks),
                source=sender.public_key.stellar_address,
                asset_issuer=self._asset_issuer if self._kin_version == 2 else None,
            )

        if channel:
            signers = [channel, sender]
        else:
            signers = [sender]

        if self.whitelist_key:
            signers.append(self.whitelist_key)

        result = self._sign_and_submit_builder(signers, builder, invoice_list)
        if result.invoice_errors:
            # Invoice errors should not be triggered on earns. This indicates there is something wrong with the service.
            raise Error('unexpected invoice errors present')

        return result

    def _sign_and_submit_builder(
        self, signers: List[PrivateKey], builder: kin_base.Builder, invoice_list: Optional[InvoiceList] = None
    ) -> SubmitTransactionResult:
        source_info = self._internal_client.get_account_info(signers[0].public_key)
        offset = 1

        def _sign_and_submit():
            nonlocal offset

            # reset generated tx and te
            builder.tx = None
            builder.te = None

            builder.sequence = source_info.sequence_number + offset
            for signer in signers:
                builder.sign(signer.stellar_seed)

            result = self._internal_client.submit_transaction(base64.b64decode(builder.gen_xdr()), invoice_list)
            if result.tx_error and isinstance(result.tx_error.tx_error, BadNonceError):
                offset += 1
                raise result.tx_error.tx_error

            return result

        return retry(self._nonce_retry_strategies, _sign_and_submit)

    def _get_stellar_builder(self, source: PrivateKey) -> kin_base.Builder:
        """Returns a Stellar transaction builder.

        :param source: The transaction source account.
        :return: a :class:`Builder` <kin_base.Builder> object.
        """
        # A Horizon instance is expected as the first argument, but it isn't used, so pass None instead to avoid
        # unnecessary aiohttp.ClientSessions getting opened.
        return kin_base.Builder(None, self.network_name,
                                100,
                                source.stellar_seed)
