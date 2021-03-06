###
# amqp.py
# AMQPFactory based on txamqp.
#
# Dan Siemon <dan@coverfire.com>
# March 2010
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
##
from twisted.internet import reactor, defer, protocol
from twisted.internet.defer import inlineCallbacks, Deferred

from txamqp.protocol import AMQClient
from txamqp.client import TwistedDelegate
from txamqp.content import Content
import txamqp

exchange_defaults = {
    'type' : 'direct',
    'durable' : True,
    'auto_delete' : False
}

queue_defaults = {
    'durable' : True,
    'exclusive' : False,
    'auto_delete' : False
}


class AMQPProtocol(AMQClient):
    """The protocol is created and destroyed each time a connection is created and lost."""
    def __init__(self, delegate, vhost, spec, prefetch_count, heartbeat, clock, insist):
        AMQClient.__init__(self, delegate, vhost, spec, heartbeat, clock, insist)
        self.prefetch_count = prefetch_count
        
    def get_consumer_tag(self):
        """Get a unique consumer tag"""
        if not hasattr(self, '_consumer_tag'):
            self._consumer_tag = 0
        self._consumer_tag += 1
        return str(self._consumer_tag)

    def connectionMade(self):
        """Called when a connection has been made."""
        AMQClient.connectionMade(self)

        # Flag that this protocol is not connected yet.
        self.connected = False

        # Authenticate.
        deferred = self.start({"LOGIN": self.factory.user, "PASSWORD": self.factory.password})
        deferred.addCallback(self._authenticated)
        deferred.addErrback(self._authentication_failed)


    def _authenticated(self, ignore):
        """Called when the connection has been authenticated."""
        # Get a channel.
        d = self.channel(1)
        d.addCallback(self._got_channel)
        d.addErrback(self._got_channel_failed)


    def _got_channel(self, chan):
        self.chan = chan

        d = self.chan.channel_open()
        d.addCallback(self._channel_open)
        d.addErrback(self._channel_open_failed)

    @inlineCallbacks
    def _channel_open(self, arg):
        """Called when the channel is open."""

        # Flag that the connection is open.
        self.connected = True

        #Limit number of messages to get
        if self.prefetch_count:
            yield self.chan.basic_qos(prefetch_count=self.prefetch_count)

        # Now that the channel is open add any readers the user has specified.
        for l in self.factory.read_list:
            self.setup_read(*l)

        # Send any messages waiting to be sent.
        self.send()

        # Fire the factory's 'initial connect' deferred if it hasn't already
        if not self.factory.initial_deferred_fired:
            self.factory.deferred.callback(self)
            self.factory.initial_deferred_fired = True

    def read(self, *argv, **kwargs):
        """Add an exchange to the list of exchanges to read from."""
        if self.connected:
            # Connection is already up. Add the reader.
            self.setup_read(*argv, **kwargs)
        else:
            # Connection is not up. _channel_open will add the reader when the
            # connection is up.
            pass

    # Send all messages that are queued in the factory.
    def send(self):
        """If connected, send all waiting messages."""
        if self.connected:
            while len(self.factory.queued_messages) > 0:
                m = self.factory.queued_messages.pop(0)
                self._send_message(*m)


    # Do all the work that configures a listener.
    @inlineCallbacks
    def setup_read(self, exchange, routing_key, callback, queue={}, no_ack=True):
        """This function does the work to read from an exchange."""
        # Use the exchange name as the queue name by default.
        if type(queue) == dict and not queue.has_key('queue'):
            if type(exchange) is dict:
                queue['queue'] = exchange['exchange']
            else:
                queue['queue'] = exchange

        # Declare the exchange in case it doesn't exist.
        exchange_cfg = dict(exchange_defaults)
        if type(exchange) is dict:
            exchange_cfg.update(exchange)
        else:
            exchange_cfg['exchange'] = exchange
        yield self.chan.exchange_declare(**exchange_cfg)

        # Get a unique consumer tag
        consumer_tag = self.get_consumer_tag()

        # Declare the queue and bind to it.
        queue_cfg = dict(queue_defaults)
        if type(queue) is dict:
            queue_cfg.update(queue)
        else:
            queue_cfg['queue'] = queue
        yield self.chan.queue_declare(**queue_cfg)
        yield self.chan.queue_bind(queue=queue_cfg['queue'], exchange=exchange_cfg['exchange'], routing_key=routing_key)

        # Consume.
        yield self.chan.basic_consume(queue=queue_cfg['queue'], no_ack=no_ack, consumer_tag=consumer_tag)
        queue = yield self.queue(consumer_tag)

        # Now setup the readers.
        d = queue.get()
        d.addCallback(self._read_item, queue, callback, no_ack)
        d.addErrback(self._read_queue_closed)
        d.addErrback(self._read_item_err)

    def _channel_open_failed(self, error):
        print "Channel open failed:", error


    def _got_channel_failed(self, error):
        print "Error getting channel:", error


    def _authentication_failed(self, error):
        print "AMQP authentication failed:", error


    @inlineCallbacks
    def _send_message(self, exchange, routing_key, msg, delivery_mode, immediate, mandatory, callback):
        """Send a single message."""
        # First declare the exchange just in case it doesn't exist.
        exchange_cfg = dict(exchange_defaults)
        if type(exchange) is dict:
            exchange_cfg.update(exchange)
        else:
            exchange_cfg['exchange'] = exchange
        yield self.chan.exchange_declare(**exchange_cfg)

        msg = Content(msg)
        msg["delivery-mode"] = delivery_mode
        d = self.chan.basic_publish(exchange=exchange_cfg['exchange'], routing_key=routing_key, content=msg, immediate=immediate, mandatory=mandatory)
        d.addErrback(self._send_message_err)

        # Chain result onto callback
        d.chainDeferred(callback)


    def _send_message_err(self, error):
        print "Sending message failed", error

    @inlineCallbacks
    def _read_item(self, item, queue, callback, no_ack):
        """Callback function which is called when an item is read."""
        # Setup another read of this queue.
        d = queue.get()
        d.addCallback(self._read_item, queue, callback, no_ack)
        d.addErrback(self._read_queue_closed)
        d.addErrback(self._read_item_err)

        # Process the read item by running the callback.
        yield callback(item)
        if not no_ack:
            yield self.chan.basic_ack(item.delivery_tag)

    def _read_queue_closed(self, failure):
        failure.trap(txamqp.queue.Closed)
        print "Queue closed"

    def _read_item_err(self, error):
        print "Error reading item: ", error


class AMQPFactory(protocol.ReconnectingClientFactory):
    protocol = AMQPProtocol


    def __init__(self, spec_file=None, vhost=None, host=None, port=None, user=None, password=None, prefetch_count=None, heartbeat=0, clock=None, insist=False, use_ssl=False, contextFactory=None):
        spec_file = spec_file or 'amqp0-8.xml'
        self.spec = txamqp.spec.load(spec_file)
        self.user = user or 'guest'
        self.password = password or 'guest'
        self.vhost = vhost or '/'
        self.host = host or 'localhost'
        self.port = port or 5672
        self.delegate = TwistedDelegate()
        self.deferred = Deferred()
        self.initial_deferred_fired = False
        self.prefetch_count = prefetch_count
        self.heartbeat = heartbeat
        self.clock = clock
        self.insist = insist

        self.p = None # The protocol instance.
        self.client = None # Alias for protocol instance

        self.queued_messages = [] # List of messages waiting to be sent.
        self.read_list = [] # List of queues to listen on.

        # Make the TCP connection.
        if use_ssl:
            if contextFactory is None:
                from twisted.internet import ssl
                contextFactory = ssl.ClientContextFactory()
            reactor.connectSSL(self.host, self.port, self, contextFactory)
        else:
            reactor.connectTCP(self.host, self.port, self)


    def buildProtocol(self, addr):
        p = self.protocol(self.delegate, self.vhost, self.spec, self.prefetch_count, self.heartbeat, self.clock, self.insist)
        p.factory = self # Tell the protocol about this factory.

        self.p = p # Store the protocol.
        self.client = p

        # Reset the reconnection delay since we're connected now.
        self.resetDelay()

        return p


    def clientConnectionFailed(self, connector, reason):
        print "Connection failed."
        protocol.ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)


    def clientConnectionLost(self, connector, reason):
        print "Client connection lost."
        self.p = None

        protocol.ReconnectingClientFactory.clientConnectionLost(self, connector, reason)


    def send_message(self, exchange=None, msg=None, routing_key="", delivery_mode=2, immediate=False, mandatory=False):
        """Send a message."""
        assert(exchange != None and msg != None)

        # Create a deferred to fire when the send completes
        sent = defer.Deferred()
        
        # Add the new message to the queue.
        self.queued_messages.append((exchange, routing_key, msg, delivery_mode, immediate, mandatory, sent))

        # This tells the protocol to send all queued messages.
        if self.p != None:
            self.p.send()

        return sent


    def read(self, exchange=None, callback=None, routing_key="", queue=None, no_ack=True):
        """Read from an exchange."""
        assert(exchange != None and callback != None)

        # Add this to the read list so that we have it to re-add if we lose the connection.
        self.read_list.append((exchange, routing_key, callback, queue, no_ack))

        # Tell the protocol to read this if it is already connected.
        if self.p != None:
            self.p.read(exchange, routing_key, callback, queue, no_ack)
