import copy
import datetime
import logging
import re
import sys
import time

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait
from slackclient import SlackClient

import settings


def handle_direct_command(command, slack_client, run_time):
    """
        Executes command caused by direct mention to thge bot
    """
    # Default response is help text for the user
    default_response = 'Not sure what you mean. Try \"stop\", \"run\" or \"last run\".'

    # Finds and executes the given command, filling in response
    response = None
    result = dict()
    # This is where you start to implement more direct mention commands
    if command == 'stop':
        response = 'Ok. We will stop'
        result['run'] = False
    elif command == 'run':
        response = 'Here we go'
        result['run'] = True
    elif command == 'last run':
        response = 'The last run was scheduled: {}'.format(run_time)
    elif command == 'reset':
        response = 'Everything is cleared'
        result['reset'] = True
    else:
        response = default_response

    post_to_slack(slack_client, response)
    return result


def parse_messages(slack_events, run_time):
    """
        Parses a list of events coming from the Slack RTM API to find bot commands.
        If a bot command is found, this function returns a tuple of command and channel.
        If its not found, then this function returns None, None.
    """
    for event in slack_events:
        if event['type'] == 'message' and 'subtype' not in event:
            result = handle_direct_command(event['text'], slack_client, run_time)
            return result
        else:
            return None


def parse_direct_mention(message_text):
    """
        Returns the userid of the user the message was directed to and returns the message itself without the mention.
        Returns None in case there weren't any direct mentions (at the beginning of the message)
    """
    matches = re.search('^<@(|[WU].+)>(.*)', message_text)
    # the first group contains the username, the second group contains the remaining message
    return (matches.group(1), matches.group(2).strip()) if matches else (None, None)


def post_to_slack(slack_client, message):
    """
        Posts a message to a slack channel using the global slack_client
    """
    if slack_client:
        slack_client.api_call(
            'chat.postMessage',
            10,
            text=message,
            type="message",
            subtype="bot_message",
            channel=settings.slack['channel']
        )


class OsmDriver(settings.driver):
    def login(self, username, password):
        self.implicitly_wait(5)
        # Log in
        self.get("http://www.onlinesoccermanager.nl/Login")
        username_element = self.find_element_by_id("manager-name")
        password_element = self.find_element_by_id("password")
        username_element.send_keys(username)
        password_element.send_keys(password)
        login_attempt = self.find_element_by_xpath("//*[@type='submit']")
        login_attempt.submit()
        info_logger.info('ingelogd')
        time.sleep(10)

        # Wacht tot hij geladen is
        found_active = self.wait_on_class('active')

        if not found_active:
            raise FileError("Kan geen actieve competitie vinden.", self)
        # Ga naar actieve competitie
        active_competition = self.find_element_by_class_name('active')
        active_competition.click()
        info_logger.info('actieve competitie gekozen')

    def wait_on_class(self, class_name):
        delay = 5  # seconds
        try:
            WebDriverWait(self, delay).until(ec.presence_of_element_located((By.CLASS_NAME, class_name)))
            success = True
        except TimeoutException:
            success = False
        return success

    def wait_on_xpath(self, xpath):
        delay = 3  # seconds
        try:
            WebDriverWait(self, delay).until(ec.presence_of_element_located((By.XPATH, xpath)))
            success = True
        except TimeoutException:
            success = False
        return success

    def go_to_url(self, link):
        self.get('http://www.onlinesoccermanager.nl/' + link)

    def read_table(self):
        html = self.page_source
        soup = BeautifulSoup(html, 'lxml')
        table = '<table>'
        table = table + str(soup.find('thead'))
        table = table + '<tbody>'
        table = table + ''.join(str(soup.find_all('tr')))
        table = table + '</tbody></table>'
        res = pd.read_html(table)[0]
        return res

    def train(self):
        self.go_to_url('Training')
        time.sleep(1)
        training_container = self.find_element_by_class_name('knockout-loader-content')
        trainingen = training_container.find_elements_by_xpath("//button[contains(., 'K')]")
        for training in trainingen:
            open_slot = True
            training.click()
            spelers = self.read_table()
            spelers_raw = copy.copy(spelers)
            spelers = spelers[spelers['Speler (Leeftijd)'] != 'Speler (Leeftijd)']
            spelers['leeftijd'] = spelers['Speler (Leeftijd)'].str[-3:-1]
            spelers = spelers.sort_values(['leeftijd'])
            clickable_spelers = self.find_elements_by_xpath('//tr[contains(@class,clickable)]')
            while open_slot:
                select = spelers['Speler (Leeftijd)'].values[0]
                correct_speler = clickable_spelers[np.where(spelers_raw['Speler (Leeftijd)'] == select)[0][0]]
                correct_speler.click()
                time.sleep(5)
                if self.find_elements_by_xpath('//h3[contains(.,"staat in de basis")]'):
                    spelers = spelers[spelers['Speler (Leeftijd)'] != select]
                    footer = self.find_element_by_class_name('modal-v2').find_element_by_class_name('modal-footer')
                    footer.find_elements_by_class_name('btn-primary')[0].click()
                    time.sleep(1)
                    if spelers.empty:
                        info_logger.info('Er kan niemand getraind worden')
                        open_slot = False
                elif self.find_elements_by_xpath('//h3[contains(.,"Je hebt niet genoeg Clubkas")]'):
                    open_slot = False
                    footer = self.find_element_by_class_name('modal-v2').find_element_by_class_name('modal-footer')
                    footer.find_elements_by_class_name('btn-primary')[1].click()
                    time.sleep(1)
                    info_logger.info('Niet genoeg geld om te trainen')
                elif not self.find_elements_by_class_name('modal-content')[0].is_displayed():
                    open_slot = False
                    info_logger.info('Speler getraind')
                    post_to_slack(slack_client, 'Speler getraind')
                    time.sleep(1)
                else:
                    open_slot = False
                    post_to_slack(slack_client, 'Is de speler getraind? Dit zou niet moeten gebeuren.')

    def rond_training_af(self, slack_client):
        self.go_to_url('Training')
        self.wait_on_class('btn-show-result')
        time.sleep(3)
        for button in self.find_elements_by_class_name('btn-show-result'):
            button.click()
            post_to_slack(slack_client, 'Speler getraind')

    def haal_bonus_op(self):
        toasts = self.find_elements_by_class_name('toastContent')
        for toast in toasts:
            toast.click()
            time.sleep(1)
            info_logger.info('Op toast geklikt')
            post_to_slack(slack_client, 'Op toast geklikt')

    def transfer_geld(self, richting):
        self.go_to_url('ControlCentre')
        time.sleep(5)
        self.wait_on_xpath("//div[@id='clubfunds-amount']")
        # Open het bank scherm
        self.find_element_by_xpath("//div[@id='clubfunds-amount']").click()
        time.sleep(5)
        self.wait_on_xpath("//span[@data-bind='currency: financePartial().interest']")
        huidige_rente = self.find_element_by_xpath("//span[@data-bind='currency: financePartial().interest']").text
        geld_op_de_bank = huidige_rente != '0'
        if richting == 'af':
            if geld_op_de_bank:
                self.find_element_by_xpath("//span[contains(., 'Overmaken')]").click()
                info_logger.info('geld van bank gehaald')
                post_to_slack(slack_client, 'Geld van de bank')
        elif richting == 'op':
            if not geld_op_de_bank:
                self.wait_on_xpath("//span[contains(., 'Overmaken')]")
                self.find_element_by_xpath("//span[contains(., 'Overmaken')]").click()
                info_logger.info('geld op de bank gezet')
                post_to_slack(slack_client, 'Geld op de bank')
            else:
                # Eerst geld eraf halen. Dan er weer op zetten
                self.transfer_geld('af')
                self.transfer_geld('op')
        else:
            FileError('Onbekende richting om geld op over te zetten.', self)
            post_to_slack(slack_client, 'Onbekende richting om geld op te zetten')

    def haal_scheidsrechter_hardheid_op(self):
        self.go_to_url('League/Fixtures')
        self.wait_on_class('highlight')
        highlight = self.find_elements_by_class_name('highlight')
        level = 0
        if highlight:
            level_class = highlight[0].find_element_by_class_name('icon-referee').get_attribute('class')
            if 'icon-referee-verylenient' in level_class:
                # groen
                level = 1
            elif 'icon-referee-lenient' in level_class:
                # blauw
                level = 2
            elif 'icon-referee-average' in level_class:
                # geel
                level = 3
            elif 'icon-referee-strict' in level_class:
                # oranje
                level = 4
            elif 'icon-referee-verystrict' in level_class:
                # rood
                level = 5
            else:
                FileError('Onbekende scheidsrechter', self)
        return level

    def zet_hardheid_goed(self):
        level = self.haal_scheidsrechter_hardheid_op()
        # level = 0 betekent dat er geen wedstrijd is.
        if level > 0:
            self.go_to_url('Tactics')
            self.wait_on_xpath('//div[@id="carousel-tacticstyleofplay"]')
            doel = ''
            if level == 5:
                doel = 'Voorzichtig'
            elif level == 4:
                doel = 'Normaal'
            elif level in [3, 2, 1]:
                doel = 'Agressief'
            correct = False
            while not correct:
                agg_element = self.find_element_by_id('carousel-tacticstyleofplay')
                huidige_level = agg_element.text
                if doel == huidige_level:
                    correct = True
                else:
                    self.wait_on_xpath('button-arrow-right')
                    time.sleep(1)
                    agg_element.find_element_by_class_name('button-arrow-right').click()
                    time.sleep(1)
            post_to_slack(slack_client, 'Scheids goed gezet')

    def selecteer_sponsor(self):
        if self.find_elements_by_class_name('icon-notification-sponsor'):
            self.go_to_url('Sponsors')
            self.wait_on_class('no-contract-container')
            contract_slots = self.find_elements_by_class_name('no-contract-container')
            for slot in contract_slots:
                slot.click()
                max_price = 0
                for i in range(0, 6):
                    price = self.find_element_by_class_name('choosesponsor-top').text.split('\n')[3][:-1]
                    if int(price) > max_price:
                        max_price = int(price)
                    self.find_element_by_class_name('carousel-next').click()
                empty = True
                while empty:
                    price = self.find_element_by_class_name('choosesponsor-top').text.split('\n')[3][:-1]
                    if int(price) == max_price:
                        self.find_element_by_xpath('//span[text()="Bevestig"]')
                        info_logger.info('Sponsor toegevoegd: {}K'.format(price))
                        post_to_slack(slack_client, 'Sponsor toegevoegd: {}K'.format(price))
                        empty = False
                    else:
                        self.find_element_by_class_name('carousel-next').click()

    def zet_specialist_goed(self):
        self.go_to_url('Specialists')
        for i in range(0, 4):
            self.wait_on_class('slidee')
            self.wait_on_class('active')
            time.sleep(2)
            active = self.find_element_by_class_name('slidee').find_element_by_class_name('active')
            if active.find_elements_by_class_name('change-player-link'):
                active.find_element_by_class_name('change-player-link').click()
            else:
                active.find_element_by_xpath('//div[text()="Kies speler"]').click()
            self.wait_on_class('table')
            time.sleep(4)
            speler_overzicht = self.read_table()
            speler_overzicht.columns = np.append('positie', speler_overzicht.columns.values[:-1])
            speler_overzicht = speler_overzicht[
                ~speler_overzicht['positie'].isin(['Aanvallers', 'Middenvelders', 'Verdedigers', 'Keepers'])]

            # Aanvoerder
            if i == 0:
                # Selecteer de oudste, en dan de eerste
                speler_overzicht['aanvoerder'] = speler_overzicht['Ver'].astype(int) / 2 + speler_overzicht[
                    'Lft'].astype(int)
                aanvoerder = speler_overzicht.sort_values(by=['aanvoerder'], ascending=False)['Aanvallers'].values[0]
                for speler in self.find_elements_by_class_name('td-player-name'):
                    if speler.text == aanvoerder:
                        speler.click()
                        time.sleep(4)
                self.find_element_by_class_name('slider-next').click()
            # Penalty
            if i == 1:
                speler_overzicht = speler_overzicht[~speler_overzicht['Aanvallers'].isin([aanvoerder])]
                speler_overzicht['penalty'] = speler_overzicht['Aan'].astype(int) / 2 + speler_overzicht[
                    'Lft'].astype(int)
                penalty = speler_overzicht.sort_values(by=['penalty'], ascending=False)['Aanvallers'].values[0]
                for speler in self.find_elements_by_class_name('td-player-name'):
                    if speler.text == penalty:
                        speler.click()
                        time.sleep(4)
                self.find_element_by_class_name('slider-next').click()
            # Vrije trappen
            if i == 2:
                speler_overzicht = speler_overzicht[~speler_overzicht['Aanvallers'].isin([aanvoerder, penalty])]
                speler_overzicht['vrij'] = speler_overzicht['Aan'].astype(int)
                vrij = speler_overzicht.sort_values(by=['vrij'], ascending=False)['Aanvallers'].values[0]
                for speler in self.find_elements_by_class_name('td-player-name'):
                    if speler.text == vrij:
                        speler.click()
                        time.sleep(4)
                self.find_element_by_class_name('slider-next').click()
            # Corners
            if i == 3:
                speler_overzicht = speler_overzicht[~speler_overzicht['Aanvallers'].isin([aanvoerder, penalty, vrij])]
                speler_overzicht['corner'] = speler_overzicht['Aan'].astype(int) + speler_overzicht['Ver'].astype(
                    int) / 5
                corner = speler_overzicht.sort_values(by=['corner'], ascending=False)['Aanvallers'].values[0]
                for speler in self.find_elements_by_class_name('td-player-name'):
                    if speler.text == corner:
                        speler.click()
                        time.sleep(4)


class FileError(Exception):
    """Custom error handling"""

    def __init__(self, message, client):
        post_to_slack(client, message)
        error_logger.error(message)
        super().__init__(message)


def create_logger(directory, name, level):
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
    root_logger = logging.getLogger(name)

    file_handler = logging.FileHandler("{0}/{1}.log".format(directory, name))
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)
    return logging.getLogger(name)


def run_script_within_try(slack_client):
    # init
    # Als gekozen voor Chrome, download eerst een chromedriver (https://sites.google.com/a/chromium.org/chromedriver/)
    if settings.driver_path:
        browser = OsmDriver(settings.driver_path)
    else:
        browser = OsmDriver()

    browser.set_window_size(1920, 1080)
    try:
        # login op osm en de juiste competitie
        browser.login(settings.username, settings.password)

        # Haal geld van de bank
        browser.transfer_geld('af')
        # Klik op afronden bij de trainingen
        browser.rond_training_af(slack_client)

        # Train speler
        browser.train()

        # Zet specialisten goed
        browser.zet_specialist_goed()

        # Zet hardheid tactiek goed (nog af te maken)
        browser.zet_hardheid_goed()

        # Klik op mogelijke bonus
        browser.haal_bonus_op()

        # Selecteer de sponsor als die bestaat
        browser.selecteer_sponsor()

        # Zet geld op de bank
        browser.transfer_geld('op')
        post_to_slack(slack_client, 'Script successfully run and browser closed')
        success = True
    except:
        # Maak een screenshot en update slack
        browser.save_screenshot('screenshot.png')
        post_to_slack(slack_client, 'Script mislukt')
        success = False
    browser.close()
    return success


def run_script(slack_client=None):
    post_to_slack(slack_client, 'Script gestart')
    iteration = 1
    finish = False
    while iteration < 5 and not finish:
        finish = run_script_within_try(slack_client)


def init_slack_client(token):
    client = SlackClient(token)
    if not client.rtm_connect(with_team_state=False):
        print('Slack kan geen verbinding maken')
        client = None
    return client


if __name__ == "__main__":
    # Logger aanmaken
    # Dit kan je oproepen overal door: info_logger.info('Dit is informatie')
    # Je kan ook een error, of een warning opgeven. Info neemt veel meer mee dan de error file.
    # De error kan je beter niet direct oproepen.
    # Maak daarvoor in de plaats een FileError want die neemt nog andere logs mee (bijv slack).
    info_logger = create_logger(settings.directory, 'info', logging.INFO)
    error_logger = create_logger(settings.directory, 'error', logging.ERROR)

    run_time = datetime.datetime.now()
    current_time = datetime.datetime.now()
    diff = current_time - run_time

    if settings.slack:
        slack_client = init_slack_client(settings.slack['token'])
    else:
        slack_client = None
    run_script(slack_client)
    run_this = True
    reset = False
    warn = False
    while True:
        current_time = datetime.datetime.now()
        diff = current_time - run_time
        time_passed = (divmod(diff.days * 86400 + diff.seconds, 3600)[0] > 7)
        if time_passed or reset:
            if run_this:
                run_script(slack_client)
                run_time = datetime.datetime.now()
                reset = False
            elif warn:
                warn = False
                post_to_slack(slack_client, 'Het is tijd om te trainen!')
        time.sleep(5)
        if slack_client:
            try:
                # Running the script closes the connection. We might need to set it up agains
                mess_res = parse_messages(slack_client.rtm_read(), run_time)
            except:
                slack_client = init_slack_client(settings.slack['token'])
                mess_res = parse_messages(slack_client.rtm_read(), run_time)
        else:
            mess_res = None
        if mess_res:
            if 'run' in mess_res:
                run_this = mess_res['run']
            if 'reset' in mess_res:
                reset = mess_res['reset']
                run_this = True
