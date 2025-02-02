import logging
import os
import requests
from requests.exceptions import HTTPError
import time
from .common import (
    get_base_url,
    get_data_url,
    get_credentials,
    get_api_version,
)
from .entity import (
    Account, AccountConfigurations, AccountActivity,
    Asset, Order, Position, BarSet, Clock, Calendar,
)
from . import polygon

logger = logging.getLogger(__name__)


class RetryException(Exception):
    pass


class APIError(Exception):
    '''Represent API related error.
    error.status_code will have http status code.
    '''

    def __init__(self, error, http_error=None):
        super().__init__(error['message'])
        self._error = error
        self._http_error = http_error

    @property
    def code(self):
        return self._error['code']

    @property
    def status_code(self):
        http_error = self._http_error
        if http_error is not None and hasattr(http_error, 'response'):
            return http_error.response.status_code

    @property
    def request(self):
        if self._http_error is not None:
            return self._http_error.request

    @property
    def response(self):
        if self._http_error is not None:
            return self._http_error.response


class REST(object):
    def __init__(
        self,
        key_id=None,
        secret_key=None,
        base_url=None,
        api_version=get_api_version,
        oauth=None
    ):
        self._key_id, self._secret_key, self._oauth = get_credentials(
            key_id, secret_key, oauth)
#        self._base_url = base_url or get_base_url()
        self._base_url = get_base_url()
        self._api_version = get_api_version(api_version)
        self._session = requests.Session()
        self._retry = int(os.environ.get('APCA_RETRY_MAX', 3))
        self._retry_wait = int(os.environ.get('APCA_RETRY_WAIT', 3))
        self._retry_codes = [int(o)for o in os.environ.get(
            'APCA_RETRY_CODES', '429,504').split(',')]
        self.polygon = polygon.REST(
            self._key_id, 'staging' in self._base_url)

    def _request(
        self,
        method,
        path,
        data=None,
        base_url=None,
        api_version=None
    ):
        base_url = base_url or self._base_url
        version = api_version if api_version else self._api_version
        url = base_url + '/' + version + path
        headers = {}
        if self._oauth:
            headers['Authorization'] = 'Bearer ' + self._oauth
        else:
            headers['APCA-API-KEY-ID'] = self._key_id
            headers['APCA-API-SECRET-KEY'] = self._secret_key
        opts = {
            'headers': headers,
            # Since we allow users to set endpoint URL via env var,
            # human error to put non-SSL endpoint could exploit
            # uncanny issues in non-GET request redirecting http->https.
            # It's better to fail early if the URL isn't right.
            'allow_redirects': False,
        }
        if method.upper() == 'GET':
            opts['params'] = data
        else:
            opts['json'] = data

        retry = self._retry
        if retry < 0:
            retry = 0
        while retry >= 0:
            try:
                return self._one_request(method, url, opts, retry)
            except RetryException:
                retry_wait = self._retry_wait
                logger.warning(
                    'sleep {} seconds and retrying {} '
                    '{} more time(s)...'.format(
                        retry_wait, url, retry))
                time.sleep(retry_wait)
                retry -= 1
                continue

    def _one_request(self, method, url, opts, retry):
        '''
        Perform one request, possibly raising RetryException in the case
        the response is 429. Otherwise, if error text contain "code" string,
        then it decodes to json object and returns APIError.
        Returns the body json in the 200 status.
        '''
        retry_codes = self._retry_codes
        resp = self._session.request(method, url, **opts)
        try:
            resp.raise_for_status()
        except HTTPError as http_error:
            # retry if we hit Rate Limit
            if resp.status_code in retry_codes and retry > 0:
                raise RetryException()
            if 'code' in resp.text:
                error = resp.json()
                if 'code' in error:
                    raise APIError(error, http_error)
            else:
                raise
        if resp.text != '':
            return resp.json()
        return None

    def get(self, path, data=None):
        return self._request('GET', path, data)

    def post(self, path, data=None):
        return self._request('POST', path, data)

    def patch(self, path, data=None):
        return self._request('PATCH', path, data)

    def delete(self, path, data=None):
        return self._request('DELETE', path, data)

    def data_get(self, path, data=None):
        base_url = get_data_url()
        return self._request(
            'GET', path, data, base_url=base_url, api_version='v2'
        )

    def get_account(self):
        '''Get the account'''
        resp = self.get('/account')
        return Account(resp)

    def get_account_configurations(self):
        '''Get account configs'''
        resp = self.get('/account/configurations')
        return AccountConfigurations(resp)

    def update_account_configurations(
        self,
        no_shorting=None,
        dtbp_check=None,
        trade_confirm_email=None,
        suspend_trade=None
    ):
        '''Update account configs'''
        params = {}
        if no_shorting is not None:
            params['no_shorting'] = no_shorting
        if dtbp_check is not None:
            params['dtbp_check'] = dtbp_check
        if trade_confirm_email is not None:
            params['trade_confirm_email'] = trade_confirm_email
        if suspend_trade is not None:
            params['suspend_trade'] = suspend_trade
        resp = self.patch('/account/configurations', params)
        return AccountConfigurations(resp)

    def list_orders(self, status=None, limit=None, after=None, until=None,
                    direction=None, params=None):
        '''
        Get a list of orders
        https://docs.alpaca.markets/web-api/orders/#get-a-list-of-orders
        '''
        if params is None:
            params = dict()
        if limit is not None:
            params['limit'] = limit
        if after is not None:
            params['after'] = after
        if until is not None:
            params['until'] = until
        if direction is not None:
            params['direction'] = direction
        if status is not None:
            params['status'] = status
        resp = self.get('/orders', params)
        return [Order(o) for o in resp]

    def submit_order(self, symbol, qty, side, type, time_in_force,
                     limit_price=None, stop_price=None, client_order_id=None,
                     extended_hours=None):
        '''Request a new order'''
        params = {
            'symbol': symbol,
            'qty': qty,
            'side': side,
            'type': type,
            'time_in_force': time_in_force,
        }
        if limit_price is not None:
            params['limit_price'] = limit_price
        if stop_price is not None:
            params['stop_price'] = stop_price
        if client_order_id is not None:
            params['client_order_id'] = client_order_id
        if extended_hours is not None:
            params['extended_hours'] = extended_hours
        resp = self.post('/orders', params)
        return Order(resp)

    def get_order_by_client_order_id(self, client_order_id):
        '''Get an order by client order id'''
        resp = self.get('/orders:by_client_order_id', {
            'client_order_id': client_order_id,
        })
        return Order(resp)

    def get_order(self, order_id):
        '''Get an order'''
        resp = self.get('/orders/{}'.format(order_id))
        return Order(resp)

    def replace_order(
        self,
        order_id,
        qty=None,
        limit_price=None,
        stop_price=None,
        time_in_force=None,
        client_order_id=None
    ):
        params = {}
        if qty is not None:
            params['qty'] = qty
        if limit_price is not None:
            params['limit_price'] = limit_price
        if stop_price is not None:
            params['stop_price'] = stop_price
        if time_in_force is not None:
            params['time_in_force'] = time_in_force
        if client_order_id is not None:
            params['client_order_id'] = client_order_id
        resp = self.patch('/orders/{}'.format(order_id), params)
        return Order(resp)

    def cancel_order(self, order_id):
        '''Cancel an order'''
        self.delete('/orders/{}'.format(order_id))

    def cancel_all_orders(self):
        '''Cancel all open orders'''
        self.delete('/orders')

    def list_positions(self):
        '''Get a list of open positions'''
        resp = self.get('/positions')
        return [Position(o) for o in resp]

    def get_position(self, symbol):
        '''Get an open position'''
        resp = self.get('/positions/{}'.format(symbol))
        return Position(resp)

    def close_position(self, symbol):
        '''Liquidates the position for the given symbol at market price'''
        self.delete('/positions/{}'.format(symbol))

    def close_all_positions(self):
        '''Liquidates all open positions at market price'''
        self.delete('/positions')

    def list_assets(self, status=None, asset_class=None):
        '''Get a list of assets'''
        params = {
            'status': status,
            'asset_class': asset_class,
        }
        resp = self.get('/assets', params)
        return [Asset(o) for o in resp]

    def get_asset(self, symbol):
        '''Get an asset'''
        resp = self.get('/assets/{}'.format(symbol))
        return Asset(resp)

    def get_barset(self,
                   symbols,
                   timeframe,
                   limit=None,
                   start=None,
                   end=None,
                   after=None,
                   until=None):
        '''Get BarSet(dict[str]->list[Bar])
        The parameter symbols can be either a comma-split string
        or a list of string. Each symbol becomes the key of
        the returned value.
        '''
        if not isinstance(symbols, str):
            symbols = ','.join(symbols)
        params = {
            'symbols': symbols,
        }
        if limit is not None:
            params['limit'] = limit
        if start is not None:
            params['start'] = start
        if end is not None:
            params['end'] = end
        if after is not None:
            params['after'] = after
        if until is not None:
            params['until'] = until
        resp = self.data_get('/bars/{}'.format(timeframe), params)
        return BarSet(resp)

    def get_clock(self):
        resp = self.get('/clock')
        return Clock(resp)

    def get_activities(
        self,
        activity_types=None,
        until=None,
        after=None,
        direction=None,
        date=None,
        page_size=None,
        page_token=None
    ):
        url = '/account/activities'
        params = {}
        if isinstance(activity_types, list):
            params['activity_types'] = ','.join(activity_types)
        elif activity_types is not None:
            url += '/{}'.format(activity_types)
        if after is not None:
            params['after'] = after
        if until is not None:
            params['until'] = until
        if direction is not None:
            params['direction'] = direction
        if date is not None:
            params['date'] = date
        if page_size is not None:
            params['page_size'] = page_size
        if page_token is not None:
            params['page_token'] = page_token
        resp = self.get(url, data=params)
        return [AccountActivity(o) for o in resp]

    def get_calendar(self, start=None, end=None):
        params = {}
        if start is not None:
            params['start'] = start
        if end is not None:
            params['end'] = end
        resp = self.get('/calendar', data=params)
        return [Calendar(o) for o in resp]
