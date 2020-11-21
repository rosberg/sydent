# -*- coding: utf-8 -*-

# Copyright 2016 OpenMarket Ltd
# Copyright 2020 Rosberg AS
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import

import logging
from base64 import b64encode
import json

from twisted.internet import defer
from sydent.http.httpclient import SimpleHttpClient
from twisted.web.http_headers import Headers

logger = logging.getLogger(__name__)


# Teletopia http JSON Api is documented here: https://www.teletopiasms.no/p/gateway/developers/api-v3-httpjson/

API_BASE_URL = "https://api1.teletopiasms.no/gateway/v3/json"

# The TON (ie. Type of Number) codes by type used in our config file
TONS = {
    'short': 1,
    'long': 4,
    'alpha': 5,
}


def tonFromType(t):
    """
    Get the type of number from the originator's type.

    :param t: Type from the originator.
    :type t: str

    :return: The type of number.
    :rtype: int
    """
    if t in TONS:
        return TONS[t]
    raise Exception("Unknown number type (%s) for originator" % t)


class TeletopiaSMS:
    def __init__(self, sydent, config_section):
        self.sydent = sydent
        self.http_cli = SimpleHttpClient(sydent)
        self.smsConfig = config_section

    @defer.inlineCallbacks
    def sendTextSMS(self, body, dest, source=None):
        """
        Sends a text message with the given body to the given MSISDN.

        :param body: The message to send.
        :type body: str
        :param dest: The destination MSISDN to send the text message to.
        :type dest: unicode
        :type source: dict[str, str] or None
        """

        username = self.smsConfig.get('username')
        password = self.smsConfig.get('password')

        if source==None:
            source = {
                "text": "Matrix",
                "type": "alpha"
            }

        body = {
                    "auth": {
                        "username": username,
                        "password": password
                    },
                    "messages": [
                        {
                            "sender": source['text'],                            
                            "senderType": tonFromType(source["type"]),
                            "recipient": dest,
                            "contentText": {
                                "text": body
                            }
                        }
                    ]
                }

        resp, responseJson = yield self.http_cli.post_json_get_body(
            "https://api1.teletopiasms.no/gateway/v3/json", body, {}
        )

        logger.info("Teletopia send sms http status code: %r", resp.code)

        if resp.code != 200:
            raise Exception("Teletopia sending sms to gateway failed with code %r", resp.code)

        s = responseJson.decode("utf8")
        respBody = json.loads(s)
        ttResponse = respBody['responses'][0]

        logger.info("Teletopia send sms to %r response.accepted          = %r", ttResponse['recipient'], ttResponse['accepted'])
        logger.info("Teletopia send sms to %r response.messageId         = %r", ttResponse['recipient'], ttResponse['messageId'])
        logger.info("Teletopia send sms to %r response.statusCode        = %r", ttResponse['recipient'], ttResponse['statusCode'])
        logger.info("Teletopia send sms to %r response.statusDescription = %r", ttResponse['recipient'], ttResponse['statusCode'])

        if ttResponse['accepted'] != 1: 
            raise Exception("Teletopia gateway did not accept sms message")
        
        if not (ttResponse['statusCode'] == 1000 or ttResponse['statusCode'] == 2000): 
            raise Exception("Teletopia gateway accepted message but reported non-successful status code = %r", ttResponse['statusCode'])
