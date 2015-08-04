from __future__ import print_function
from datetime import date, timedelta
import logging
from collections import Counter

import yaml
import numpy as np

from pcse.base_classes import DispatcherObject, VariableKiosk, SimulationObject, ParameterProvider, AncillaryObject
from pcse.traitlets import HasTraits, Float, Int, Instance, Enum, Bool, List, Dict, Unicode
from pcse import exceptions as exc
from pcse.util import ConfigurationLoader


def check_date_range(day, start, end):
    """returns True if start <= day < end

    Optionally, d3 may be None. in that case return True if d1 <= d2

    :param day: the date that will be checked
    :param start: the start date of the range
    :param end: the end date of the range or None
    :return: Boolean True/False
    """

    if end is None:
        return start <= day
    else:
        return start <= day < end


class CropCalendar(HasTraits, DispatcherObject):
    """A crop calendar for managing the crop cycle.

    A `CropCalendar` object is responsible for storing, checking, initiating and ending
    the crop cycle. The crop calendar is initialized by providing the parameters needed
    for defining the crop cycle. At each time step the instance of `CropCalendar` is called
    and at dates defined by its parameters initiates the appropriate actions::
    - sowing/emergence: the crop simulation is initiated based on the model found in the model
      configuration (e.g. self.mconf.CROP). This instance is then transmitted to the engine
      by dispatching the signal `signals.crop_start`.
    - maturity/harvest: the crop cycle is ended by

    :param kiosk:           The PCSE VariableKiosk instance
    :param crop_id:         String identifying the crop
    :param crop_start_date: Start date of the crop simulation
    :param crop_start_type: Start type of the crop simulation ('sowing', 'emergence')
    :param crop_end_date: End date of the crop simulation
    :param crop_end_type: End type of the crop simulation ('harvest', 'maturity', 'earliest')
    :param max_duration: Integer describing the maximum duration of the crop cycle

    :return: A CropCalendar Instance
    """

    # Characteristics of the crop cycle
    crop_id = Unicode()
    crop_start_date = Instance(date)
    crop_start_type = Enum(["sowing", "emergence"])
    crop_end_date = Instance(date)
    crop_end_type = Enum(["maturity", "harvest", "earliest"])
    max_duration = Int()

    # system parameters
    kiosk = Instance(VariableKiosk)
    parameterprovider = Instance(ParameterProvider)
    mconf = Instance(ConfigurationLoader)
    logger = Instance(logging.Logger)

    # Counter for duration of the crop cycle
    duration = Int(0)
    in_crop_cycle = Bool(False)

    def __init__(self, kiosk, crop_id=None, crop_start_date=None,
                 crop_start_type=None, crop_end_date=None, crop_end_type=None, max_duration=None):

        # set up logging
        loggername = "%s.%s" % (self.__class__.__module__,
                                self.__class__.__name__)

        self.logger = logging.getLogger(loggername)
        self.kiosk = kiosk
        self.crop_id = crop_id
        self.crop_start_date = crop_start_date
        self.crop_start_type = crop_start_type
        self.crop_end_date = crop_end_date
        self.crop_end_type = crop_end_type
        self.max_duration = max_duration

        self._connect_signal(self._on_CROP_FINISH, signal=signals.crop_finish)

    def validate(self, campaign_start_date, next_campaign_start_date):
        """Validate the crop calendar internally and against the interval for
        the agricultural campaign

        :param campaign_start_date: start date of this campaign
        :param next_campaign_start_date: start date of the next campaign
        """

        # Check that crop_start_date is before crop_end_date
        crop_end_date = self.crop_end_date
        if self.crop_end_type == "maturity":
            crop_end_date = self.crop_start_date + timedelta(days=self.max_duration)
        if self.crop_start_date >= crop_end_date:
            msg = "crop_end_date before or equal to crop_start_date for crop '%s'!"
            raise exc.PCSEError(msg % (self.crop_start_date, self.crop_end_date))

        # check that crop_start_date is within the campaign interval
        r = check_date_range(self.crop_start_date, campaign_start_date, next_campaign_start_date)
        if r is not True:
            msg = "Start date (%s) for crop '%s' not within campaign window (%s - %s)." % \
                  (self.crop_start_date, self.crop_id, campaign_start_date, next_campaign_start_date)
            raise exc.PCSEError(msg)

    def __call__(self, day, drv):

        if self.in_crop_cycle:
            self.duration += 1

        # Start of the crop cycle
        if day == self.crop_start_date:  # Start a new crop
            self.duration = 0
            self.in_crop_cycle = True
            msg = "Starting crop (%s) on day %s" % (self.crop_id, day)
            self.logger.info(msg)
            self._send_signal(signal=signals.crop_start, day=day,
                              crop_id=self.crop_id, crop_start_type=self.crop_start_type,
                              crop_end_type=self.crop_end_type)

        # end of the crop cycle
        finish_type = None
        # Check if crop_end_date is reached for CROP_END_TYPE harvest/earliest
        if self.crop_end_type in ["harvest", "earliest"]:
            if day >= self.crop_end_date:
                finish_type = "harvest"

        # Check for forced stop because maximum duration is reached
        if self.duration >= self.max_duration:
            finish_type = "max_duration"

        # If finish condition is reached send a signal to finish the crop
        if finish_type is not None:
            self.in_crop_cycle = False
            self._send_signal(signal=signals.crop_finish, day=day,
                              finish=finish_type, crop_delete=True)

    def _on_CROP_FINISH(self):
        """Register that crop has reached the end of its cycle.
        """
        self.in_crop_cycle = False

    def get_end_date(self):
        """Return the end date of the crop cycle.

        This is either given as the harvest date or calculated as
        crop_start_date + max_duration

        :return: a date object
        """
        if self.crop_end_type in ["harvest", 'earliest']:
            return self.crop_end_date
        else:
            return self.crop_start_date + timedelta(days=self.max_duration)

    def get_start_date(self):
        """Returns the start date of the cycle. This is always self.crop_start_date

        :return: the start date
        """
        return self.crop_start_date


class TimedEventsDispatcher(HasTraits, DispatcherObject):
    event_signal = None
    events_table = List()
    days_with_events = Instance(Counter)
    kiosk = Instance(VariableKiosk)
    logger = Instance(logging.Logger)
    name = Unicode()
    comment = Unicode()

    def __init__(self, kiosk, event_signal, name, comment, events_table):

        # set up logging
        loggername = "%s.%s" % (self.__class__.__module__,
                                self.__class__.__name__)
        self.logger = logging.getLogger(loggername)

        self.kiosk = kiosk
        self.events_table = events_table
        self.name = name
        self.comment = comment

        # get signal from signals module
        if not hasattr(signals, event_signal):
            msg = "Signal '%s'  not defined in pcse.signals module."
            raise exc.PCSEError(msg % event_signal)
        # self.event_signal = getattr(signals, event_signal)
        self.event_signal = getattr(signals, event_signal)

        # Build a counter for the days with events.
        self.days_with_events = Counter()
        for ev in self.events_table:
            self.days_with_events.update(ev.keys())

        # Check if there are days with two or more events under the
        # same signal which is not allowed.
        multi_days = []
        for day, count in self.days_with_events.items():
            if count > 1:
                multi_days.append(day)
        if multi_days:
            msg = "Found days with more than 1 event for events table '%s' on days: %s"
            raise exc.PCSEError(msg % (self.name, multi_days))

    def validate(self, campaign_start_date, next_campaign_start_date):
        for event in self.events_table:
            day = event.keys()[0]
            r = check_date_range(day, campaign_start_date, next_campaign_start_date)
            if r is not True:
                msg = "Timed event at day %s not in campaign interval (%s - %s)" %\
                      (day, campaign_start_date, next_campaign_start_date)
                raise exc.PCSEError(msg)

    def __call__(self, day):
        if day not in self.days_with_events:
            return

        for event in self.events_table:
            if day in event:
                msg = "Time event dispatched from '%s' at day %s" % (self.name, day)
                self.logger.info(msg)
                kwargs = event[day]
                self._send_signal(signal=self.event_signal, **kwargs)

    def get_end_date(self):
        """Returns the last date for which a timed event is given
        """
        return max(self.days_with_events)


class StateEventsDispatcher(HasTraits, DispatcherObject):
    event_signal = None
    event_state = Unicode()
    zero_condition = Enum(['rising', 'falling', 'either'])
    events_table = List()
    kiosk = Instance(VariableKiosk)
    logger = Instance(logging.Logger)
    name = Unicode()
    comment = Unicode()
    previous_signs = List()

    def __init__(self, kiosk, event_signal, event_state, zero_condition, name,
                 comment, events_table):

        # set up logging
        loggername = "%s.%s" % (self.__class__.__module__,
                                self.__class__.__name__)
        self.logger = logging.getLogger(loggername)

        self.kiosk = kiosk
        self.events_table = events_table
        self.zero_condition = zero_condition
        self.event_state = event_state
        self.name = name
        self.comment = comment

        # assign evaluation function for states
        if self.zero_condition == 'falling':
            self._evaluate_state = self._zero_condition_falling
        elif self.zero_condition == 'rising':
            self._evaluate_state = self._zero_condition_rising
        elif self.zero_condition == 'either':
            self._evaluate_state = self._zero_condition_either

        # assign Nones to self.zero_condition_signs to signal
        # that the sign have not yet been evaluated
        self.previous_signs = [None]*len(self.events_table)

        # get signal from signals module
        if not hasattr(signals, event_signal):
            msg = "Signal '%s' not defined in pcse.signals module."
            raise exc.PCSEError(msg % event_signal)
        self.event_signal = getattr(signals, event_signal)

        # Build a counter for the state events.
        self.states_with_events = Counter()
        for ev in self.events_table:
            self.states_with_events.update(ev.keys())

        # Check if there are days with two or more events under the
        # same signal which is not allowed.
        multi_states = []
        for state, count in self.states_with_events.items():
            if count > 1:
                multi_states.append(state)
        if multi_states:
            msg = "Found states with more than 1 event for events table '%s' for state: %s"
            raise exc.PCSEError(msg % (self.name, multi_states))

    def __call__(self, day):
        if not self.event_state in self.kiosk:
            msg = "State variable '%s' not (yet) available in kiosk!" % self.event_state
            self.logger.warning(msg)
            return

        # Determine if any event should be trigger based on the current state and
        # the event_condition.
        current_state = self.kiosk[self.event_state]
        zero_condition_signs = []
        for event, zero_condition_sign in zip(self.events_table, self.previous_signs):
            state, keywords = event.items()[0]
            zcs = self._evaluate_state(current_state, state, keywords, zero_condition_sign)
            zero_condition_signs.append(zcs)
        self.previous_signs = zero_condition_signs


    def _zero_condition_falling(self, current_state, state, keywords, zero_condition_sign):
        sign = cmp(current_state - state, 0)

        # is None: e.g. called the first time and zero_condition_sign is not yet calculated
        if zero_condition_sign is None:
            return sign

        if zero_condition_sign == 1 and sign in [-1, 0]:
            msg = "State event dispatched from '%s' at event_state %s" % (self.name, state)
            self.logger.info(msg)
            self._send_signal(signal=self.event_signal, **keywords)

        return sign

    def _zero_condition_rising(self, current_state, state, kwargs, zero_condition_sign):
        sign = cmp(current_state - state, 0)

        # is None: e.g. called the first time and zero_condition_sign is not yet calculated
        if zero_condition_sign is None:
            return sign

        if zero_condition_sign == -1 and sign in [0, 1]:
            msg = "State event dispatched from '%s' at model state %s" % (self.name, current_state)
            self.logger.info(msg)
            self._send_signal(signal=self.event_signal, **kwargs)

        return sign

    def _zero_condition_either(self, current_state, state, keywords, zero_condition_sign):
        sign = cmp(current_state - state, 0)

        # is None: e.g. called the first time and zero_condition_sign is not yet calculated
        if zero_condition_sign is None:
            return sign

        if (zero_condition_sign == 1 and sign in [-1, 0]) or \
           (zero_condition_sign == -1 and sign in [0, 1]):
            msg = "State event dispatched from %s at event_state %s" % (self.name, state)
            self.logger.info(msg)
            self._send_signal(signal=self.event_signal, **keywords)

        return sign


class AgroManager(AncillaryObject):

    # campaign start dates
    campaign_start_dates = List()

    # Overall engine start date and end date
    _start_date = Instance(date)
    _end_date = Instance(date)

    # campaign definitions
    crop_calendars = List()
    timed_event_dispatchers = List()
    state_event_dispatchers = List()

    _tmp_date = None  # Helper variable
    _icampaign = 0  # count the campaigns

    def initialize(self, kiosk, agromanagement):

        self.kiosk = kiosk
        self.crop_calendars = []
        self.timed_event_dispatchers = []
        self.state_event_dispatchers = []
        self.campaign_start_dates = []

        # First get and validate the dates of the different campaigns
        for campaign in agromanagement:
            # Check if campaign start dates is in chronological order
            campaign_start_date = campaign.keys()[0]
            self._check_campaign_date(campaign_start_date)
            self.campaign_start_dates.append(campaign_start_date)

        # Add None to the list of campaign dates to signal the end of the
        # number of campaigns.
        self.campaign_start_dates.append(None)

        # Walk through the different campaigns and build crop calendars and
        # timed/state event dispatchers
        for campaign, campaign_start, next_campaign in \
                zip(agromanagement, self.campaign_start_dates[:-1], self.campaign_start_dates[1:]):

            # Get the campaign definition for the start date
            campaign_def = campaign[campaign_start]

            if campaign_def is None:  # no campaign definition for this campaign, e.g. fallow
                self.crop_calendars.append(None)
                self.timed_event_dispatchers.append(None)
                self.state_event_dispatchers.append(None)
                continue

            # get crop calendar definition for this campaign
            cc_def = campaign_def['CropCalendar']
            if cc_def is not None:
                cc = CropCalendar(kiosk, **cc_def)
                cc.validate(campaign_start, next_campaign)
                self.crop_calendars.append(cc)
            else:
                self.crop_calendars.append(None)

            # Get definition of timed events and build TimedEventsDispatchers
            te_def = campaign_def['TimedEvents']
            if te_def is not None:
                te_dsp = self._build_TimedEventDispatchers(kiosk, te_def)
                for te in te_dsp:
                    te.validate(campaign_start, next_campaign)
                self.timed_event_dispatchers.append(te_dsp)
            else:
                self.timed_event_dispatchers.append(None)

            # Get definition of state events and build StateEventsDispatchers
            se_def = campaign_def['StateEvents']
            if se_def is not None:
                se_dsp = self._build_StateEventDispatchers(kiosk, se_def)
                self.state_event_dispatchers.append(se_dsp)
            else:
                self.state_event_dispatchers.append(None)

    @property
    def start_date(self):
        """Retrieves the start date of the agromanagement sequence, e.g. the first simulation date

        :return: a date object
        """
        if self._start_date is None:
            self._start_date = self.campaign_start_dates[0]

        return self._start_date

    @property
    def end_date(self):
        """Retrieves the end date of the agromanagement sequence, e.g. the last simulation date.

        Getting the last simulation date is more complicated because it depends on end date of the
        crop calendar and possible timed events which are scheduled in that campaign.
        In practice the end date of all CropCalendar and TimedEventDispatchers objects is retrieved
        by iterating over them. Then the maximum value of the end dates is taken. This approach
        has the advantage that any trailing empty campaigns are removed.

        Two examples of "trailing empty campaigns" in the agromanagement file (YAML format) are::

            2001-01-01:
                CropCalendar: null
                TimedEvents: null
                StateEvents: null
            2002-01-01: null

        :return: a date object
        """
        if self._end_date is None:

            cc_dates = []
            te_dates = []
            for cc, teds in zip(self.crop_calendars, self.timed_event_dispatchers):
                if cc is not None:
                    cc_dates.append(cc.get_end_date())
                if teds is not None:
                    te_dates.extend([t.get_end_date() for t in teds])

            # If not end dates can be found raise and error because the agromanagement sequnce
            # consists only of empty campaigns
            if not cc_dates and not te_dates:
                msg = "Empty agromanagement definition: no campaigns with crop calendars or timed events provided!"
                raise exc.PCSEError(msg)

            end_date = date(1,1,1)
            if cc_dates:
                end_date = max(max(cc_dates), end_date)
            if te_dates:
                end_date = max(max(te_dates), end_date)
            self._end_date = end_date

        return self._end_date

    def _check_campaign_date(self, campaign_start_date):
        """
        :param campaign_start_date: Start date of the agricultural campaign
        :return: None
        """
        if not isinstance(campaign_start_date, date):
            msg = "Campaign start must be given as a date."
            raise exc.PCSEError(msg)

        if self._tmp_date is None:
            self._tmp_date = campaign_start_date
        else:
            if campaign_start_date <= self._tmp_date:
                msg = "Definition of agricultural campaigns is not sequential " \
                      "in definition of agromanagement"
                raise exc.PCSEError(msg)

    def _build_TimedEventDispatchers(self, kiosk, event_definitions):
        r = []
        for ev_def in event_definitions:
            ev_dispatcher = TimedEventsDispatcher(kiosk, **ev_def)
            r.append(ev_dispatcher)
        return r

    def _build_StateEventDispatchers(self, kiosk, event_definitions):
        r = []
        for ev_def in event_definitions:
            ev_dispatcher = StateEventsDispatcher(kiosk, **ev_def)
            r.append(ev_dispatcher)
        return r

    def __call__(self, day, drv):

        # Check if the agromanager should switch to a new campaign
        if day == self.campaign_start_dates[self._icampaign+1]:
            self._icampaign += 1
            # if new campaign, throw out the previous campaign definition
            self.crop_calendars.pop(0)
            self.timed_event_dispatchers.pop(0)
            self.state_event_dispatchers.pop(0)

        # call handlers for the crop calendar, timed and state events
        if self.crop_calendars[0] is not None:
            self.crop_calendars[0](day, drv)

        if self.timed_event_dispatchers[0] is not None:
            for ev_dsp in self.timed_event_dispatchers[0]:
                ev_dsp(day)

        if self.state_event_dispatchers[0] is not None:
            for ev_dsp in self.state_event_dispatchers[0]:
                ev_dsp(day)

    def _on_CROP_FINISH(self):
        """Send signal to terminate if the number of campaigns is exhausted.
        """
        if not self.crop_calendars:
            self._send_signal(signal=signals.terminate)


class TestSignals(object):
    apply_npk = "APPLY_NPK"
    irrigate = "IRRIGATE"
    crop_finish = "CROP_FINISH"
    crop_start = "CROP_START"
signals = TestSignals()

class MyModel(SimulationObject):
    def initialize(self, day, kiosk, *args):

        self._connect_signal(self._on_SIGNAL, signals.apply_npk)
        self._connect_signal(self._on_SIGNAL, signals.irrigate)

    def _on_SIGNAL(self, signal, sender, **kwargs):
        msg = "signal %s received with args: %s" % (signal, kwargs)
        print(msg)

def main():
    r = yaml.load(open("events2.yaml"))

    kiosk = VariableKiosk()
    kiosk.register_variable(1, "DVS", type="S", publish=True)
    kiosk.register_variable(1, "SM", type="S", publish=True)


#    mconf = ConfigurationLoader(r"D:\UserData\sources\pcse\pcse\pcse\conf\Wofost71_PP.conf")
#    parameterprovider = ParameterProvider({},{},{},{})

    # Start the agromanager
    agromanager = AgroManager(kiosk, r["AgroManagement"])
    start_date = agromanager.start_date
    end_date = agromanager.end_date

    # register a model that listens to signals sent by events.
    model = MyModel(start_date, kiosk)

    # The 'simulation loop'
    doys = range(100)
    development = np.arange(0,2,0.02)
    np.random.seed(1000)
    soil_moisture = np.random.rand(200)
    drv = None

    for doy, dvs, sm in zip(doys, development, soil_moisture):

        day = start_date + timedelta(days=doy)
        kiosk.set_variable(1, "DVS", dvs)
        kiosk.set_variable(1, "SM", sm)
        print("day: %s, DVS: %5.2f, SM: %5.3f" % (day, dvs, sm))
        agromanager(day, drv)



if __name__ == "__main__":
    main()