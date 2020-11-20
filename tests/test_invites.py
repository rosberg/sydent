from mock import Mock
from sydent.http.httpclient import FederationHttpClient
from sydent.db.invite_tokens import JoinTokenStore
from tests.utils import make_sydent
from twisted.web.client import Response
from twisted.trial import unittest
from sydent.http.servlets.store_invite_servlet import StoreInviteServlet


class ThreepidInvitesTestCase(unittest.TestCase):
    """Tests features related to storing and delivering 3PID invites."""

    def setUp(self):
        # Create a new sydent
        config = {
            "email": {
                # Used by test_invited_email_address_obfuscation
                "email.third_party_invite_username_reveal_characters": "6",
                "email.third_party_invite_domain_reveal_characters": "8",
            },
        }
        self.sydent = make_sydent(test_config=config)

    def test_delete_on_bind(self):
        """Tests that 3PID invite tokens are deleted upon delivery after a successful
        bind.
        """
        self.sydent.run()

        # The 3PID we're working with.
        medium = "email"
        address = "john@example.com"

        # Mock post_json_get_nothing so the /onBind call doesn't fail.
        def post_json_get_nothing(uri, post_json, opts):
            return Response((b'HTTP', 1, 1), 200, b'OK', None, None)

        FederationHttpClient.post_json_get_nothing = Mock(
            side_effect=post_json_get_nothing,
        )

        # Manually insert an invite token, we'll check later that it's been deleted.
        join_token_store = JoinTokenStore(self.sydent)
        join_token_store.storeToken(
            medium, address, "!someroom:example.com", "@jane:example.com",
            "sometoken",
        )

        # Make sure the token still exists and can be retrieved.
        tokens = join_token_store.getTokens(medium, address)
        self.assertEqual(len(tokens), 1, tokens)

        # Bind the 3PID
        self.sydent.threepidBinder.addBinding(
            medium, address, "@john:example.com", check_info=False,
        )

        # Give Sydent some time to call /onBind and delete the token.
        self.sydent.reactor.advance(1000)

        cur = self.sydent.db.cursor()

        # Manually retrieve the tokens for this 3PID. We don't use getTokens because it
        # filters out sent tokens, so would return nothing even if the token hasn't been
        # deleted.
        res = cur.execute(
            "SELECT medium, address, room_id, sender, token FROM invite_tokens"
            " WHERE medium = ? AND address = ?",
            (medium, address,)
        )
        rows = res.fetchall()

        # Check that we didn't get any result.
        self.assertEqual(len(rows), 0, rows)

    def test_invited_email_address_obfuscation(self):
        """Test that email addresses included in third-party invites are properly
        obfuscated according to the relevant config options
        """
        store_invite_servlet = StoreInviteServlet(self.sydent)

        email_address = "1234567890@1234567890.com"
        redacted_address = store_invite_servlet.redact_email_address(email_address)

        self.assertEqual(redacted_address, "123456...@12345678...")

        # Even short addresses are redacted
        short_email_address = "1@1.com"
        redacted_address = store_invite_servlet.redact_email_address(short_email_address)

        self.assertEqual(redacted_address, "...@1...")

        # Try using a username separator string
        self.sydent.third_party_invite_username_separator_string = "-"
        email_address = "johnathon-jingle-smithington@john-smith.notarealtld"
        redacted_address = store_invite_servlet.redact_email_address(email_address)
        # Each individual component of the username should be obfuscated, but not the domain
        self.assertEqual(redacted_address, "johnat...-jin...-smithi...@john-smi...")

        # Try one with a separator at a word boundary
        email_address = "applejack-@someexample.com"
        redacted_address = store_invite_servlet.redact_email_address(email_address)
        self.assertEqual(redacted_address, "applej...-@someexam...")

        # Try one where the username is just the separator.
        email_address = "-@someexample.com"
        redacted_address = store_invite_servlet.redact_email_address(email_address)
        self.assertEqual(redacted_address, "-@someexam...")

        # Try multiple, sequential separators
        self.sydent.username_reveal_characters = 3
        self.sydent.domain_reveal_characters = 3

        email_address = "donuld--fauntleboy--puck@disnie.com"
        redacted_address = store_invite_servlet.redact_email_address(email_address)
        self.assertEqual(redacted_address, "don...--fau...--puc...@dis...")

class ThreepidInvitesFallbackConfigTestCase(unittest.TestCase):
    """Tests that any fallback config options work."""

    def setUp(self):
        # Create a new sydent
        config = {
            "email": {
                # Test that the values of fallback config options are still used when their
                # equivalent and new counterparts are not set

                # Fallback of email.third_party_invite_username_reveal_characters
                "email.third_party_invite_username_obfuscate_characters": "9",
                # Fallback of email.third_party_invite_domain_reveal_characters
                "email.third_party_invite_domain_obfuscate_characters": "4",
            },
        }
        self.sydent = make_sydent(test_config=config)

    def test_invited_email_address_obfuscation_fallback_config(self):
        """Test fallback options relating to email address obfuscation"""
        store_invite_servlet = StoreInviteServlet(self.sydent)

        email_address = "1234567890@1234567890.com"
        redacted_address = store_invite_servlet.redact_email_address(email_address)

        self.assertEqual(redacted_address, "123456789...@1234...")


class ThreepidInvitesNoDeleteTestCase(unittest.TestCase):
    """Test that invite tokens are not deleted when that is disabled.
    """

    def setUp(self):
        # Create a new sydent
        config = {
            "general": {
                "delete_tokens_on_bind": "false"
            }
        }
        self.sydent = make_sydent(test_config=config)

    def test_no_delete_on_bind(self):
        self.sydent.run()

        # The 3PID we're working with.
        medium = "email"
        address = "john@example.com"

        # Mock post_json_get_nothing so the /onBind call doesn't fail.
        def post_json_get_nothing(uri, post_json, opts):
            return Response((b'HTTP', 1, 1), 200, b'OK', None, None)

        FederationHttpClient.post_json_get_nothing = Mock(
            side_effect=post_json_get_nothing,
        )

        # Manually insert an invite token, we'll check later that it's been deleted.
        join_token_store = JoinTokenStore(self.sydent)
        join_token_store.storeToken(
            medium, address, "!someroom:example.com", "@jane:example.com",
            "sometoken",
        )

        # Make sure the token still exists and can be retrieved.
        tokens = join_token_store.getTokens(medium, address)
        self.assertEqual(len(tokens), 1, tokens)

        # Bind the 3PID
        self.sydent.threepidBinder.addBinding(
            medium, address, "@john:example.com", check_info=False,
        )

        # Give Sydent some time to call /onBind and delete the token.
        self.sydent.reactor.advance(1000)

        cur = self.sydent.db.cursor()

        # Manually retrieve the tokens for this 3PID. We don't use getTokens because it
        # filters out sent tokens, so would return nothing even if the token hasn't been
        # deleted.
        res = cur.execute(
            "SELECT medium, address, room_id, sender, token FROM invite_tokens"
            " WHERE medium = ? AND address = ?",
            (medium, address,)
        )
        rows = res.fetchall()

        # Check that we didn't get any result.
        self.assertEqual(len(rows), 1, rows)
