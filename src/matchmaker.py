#!/usr/bin/env python3

import argparse
import datetime
import logging

import config
import currency
import database
import donationswap
import entities
import mail
import util

class Matchmaker:

	def __init__(self, config_path, dry_run=False):
		self._dry_run = dry_run
		self._config = config.Config(config_path)
		self._database = database.Database(self._config.db_connection_string)
		self._currency = currency.Currency(self._config.currency_cache, self._config.fixer_apikey)
		self._mail = mail.Mail(self._config.email_user, self._config.email_password, self._config.email_smtp, self._config.email_sender_name)

		with self._database.connect() as db:
			entities.load(db)

	def clean(self):
		now = datetime.datetime.utcnow()
		two_days = datetime.timedelta(days=2)
		one_week = datetime.timedelta(days=7)
		four_weeks = datetime.timedelta(days=28)

		with self._database.connect() as db:

			# delete unconfirmed offers after 48 hours
			for offer in entities.Offer.get_all(lambda x: not x.confirmed and x.created_ts + two_days < now):
				offer.delete(db)

			# delete declined matches immediately
			for match in entities.Match.get_all(lambda x: x.new_agrees is False or x.old_agrees is False):
				match.delete(db)

			# delete unapproved matches after one week
			for match in entities.Match.get_all(lambda x: x.new_agrees is None or x.old_agrees is None and x.created_ts + one_week < now):
				match.delete(db)

			# delete approved matches after four weeks
			for match in entities.Match.get_all(lambda x: x.new_agrees is True and x.old_agrees is True and x.created_ts + four_weeks < now):
				match.delete(db)

			#xxx also...
			# ... delete expired offers that aren't part of a match
			# ... signal the web server to update its cache

	@staticmethod
	def _is_good_match(offer1, offer2):
		logging.info('Comparing %s and %s.', offer1.id, offer2.id)

		if offer1.charity_id == offer2.charity_id:
			logging.info('same charity.')
			return False
		if offer1.country_id == offer2.country_id:
			logging.info('same country.')
			return False
		if offer1.email == offer2.email:
			logging.info('same email.')
			return False

		#xxx offers SHOULD have approximately the same amount (taking tax benefits into account)
		#    for development, however, everthing goes.
		return True

	def find_matches(self):
		'''Compares every offer to every other offer.'''

		matches = []

		with self._database.connect() as db:
			offers = entities.Offer.get_match_candidates(db)

		logging.info('There are %s eligible offers to match up.', len(offers))

		while offers:
			offer1 = offers.pop()
			for offer2 in offers:
				if self._is_good_match(offer1, offer2):
					matches.append((offer1, offer2))
					offers.remove(offer2)
					break

		logging.info('Found %s matching pairs.', len(matches))

		return matches

	def _send_mail_about_match(self, my_offer, their_offer, match_secret):
		your_amount_in_their_currency = self._currency.convert(
			their_offer.amount,
			their_offer.country.currency.iso,
			my_offer.country.currency.iso)

		replacements = {
			'{%YOUR_COUNTRY%}': my_offer.country.name,
			'{%YOUR_CHARITY%}': my_offer.charity.name,
			'{%YOUR_AMOUNT%}': my_offer.amount,
			'{%YOUR_CURRENCY%}': my_offer.country.currency.iso,
			'{%THEIR_COUNTRY%}': their_offer.country.name,
			'{%THEIR_CHARITY%}': their_offer.charity.name,
			'{%THEIR_AMOUNT%}': their_offer.amount,
			'{%THEIR_CURRENCY%}': their_offer.country.currency.iso,
			'{%THEIR_AMOUNT_CONVERTED%}': your_amount_in_their_currency,
			'{%SECRET%}': '%s%s' % (my_offer.secret, match_secret),
			# Do NOT put their email address here.
			# Wait until both parties approved the match.
		}

		logging.info('Sending match email to %s.', my_offer.email)
		self._mail.send(
			'We may have found a matching donation for you',
			util.Template('match-suggested-email.txt').replace(replacements).content,
			html=util.Template('match-suggested-email.html').replace(replacements).content,
			to=my_offer.email
		)

	def process_found_matches(self, matches):
		for (offer1, offer2) in matches:
			if self._dry_run:
				logging.info('Doing nothing, because this is a dry run; offer1=%s; offer2=%s.', offer1, offer2)
				continue

			match_secret = donationswap.create_secret()

			if offer1.created_ts < offer2.created_ts:
				old_offer, new_offer = offer1, offer2
			else:
				old_offer, new_offer = offer2, offer1

			logging.info('Creating match between offers %s and %s.', new_offer.id, old_offer.id)
			with self._database.connect() as db:
				entities.Match.create(db, match_secret, new_offer.id, old_offer.id)

			self._send_mail_about_match(old_offer, new_offer, match_secret)
			self._send_mail_about_match(new_offer, old_offer, match_secret)

	def _send_mail_about_deal(self, old_offer, new_offer):
		old_amount_in_new_currency = self._currency.convert(
			old_offer.amount,
			old_offer.country.currency.iso,
			new_offer.country.currency.iso)
		new_amount_in_old_currency = self._currency.convert(
			new_offer.amount,
			new_offer.country.currency.iso,
			old_offer.country.currency.iso)

		tmp = entities.CharityInCountry.by_charity_and_country_id(new_offer.charity.id, new_offer.country.id)
		if tmp is not None:
			old_instructions = tmp.instructions
		else:
			old_instructions = 'Sorry, there are no instructions available (yet).'

		tmp = entities.CharityInCountry.by_charity_and_country_id(old_offer.charity.id, new_offer.country.id)
		if tmp is not None:
			new_instructions = tmp.instructions
		else:
			new_instructions = 'Sorry, there are no instructions available (yet).'

		replacements = {
			'{%OLD_COUNTRY%}': old_offer.country.name,
			'{%OLD_CHARITY%}': old_offer.charity.name,
			'{%OLD_AMOUNT%}': old_offer.amount,
			'{%OLD_CURRENCY%}': old_offer.country.currency.iso,
			'{%OLD_EMAIL%}': old_offer.email,
			'{%OLD_AMOUNT_CONVERTED%}': old_amount_in_new_currency,
			'{%OLD_INSTRUCTIONS%}': old_instructions,
			'{%NEW_COUNTRY%}': new_offer.country.name,
			'{%NEW_CHARITY%}': new_offer.charity.name,
			'{%NEW_AMOUNT%}': new_offer.amount,
			'{%NEW_CURRENCY%}': new_offer.country.currency.iso,
			'{%NEW_EMAIL%}': new_offer.email,
			'{%NEW_AMOUNT_CONVERTED%}': new_amount_in_old_currency,
			'{%NEW_INSTRUCTIONS%}': new_instructions,
		}

		logging.info('Sending deal email to %s and %s.', old_offer.email, new_offer.email)
		self._mail.send(
			'Here is your match!',
			util.Template('match-approved-email.txt').replace(replacements).content,
			html=util.Template('match-approved-email.html').replace(replacements).content,
			to=[old_offer.email, new_offer.email]
		)

	def process_approved_matches(self):
		approved_matches = entities.Match.get_all(lambda x: x.new_agrees and x.old_agrees)
		for match in approved_matches:
			if self._dry_run:
				logging.info('Doing nothing, because this is a dry run; match=%s.', match)
				continue

			self._send_mail_about_deal(match.old_offer, match.new_offer)
			#xxx do not delete completed match for 1 month
			#xxx make site with these instructions, put URL in email.
			with self._database.connect() as db:
				match.delete(db)

def main():
	util.setup_logging('log/matchmaker.txt')
	parser = argparse.ArgumentParser(description='The Match Maker.')
	parser.add_argument('config_path')
	parser.add_argument('--doit', action='store_true')
	args = parser.parse_args()

	matchmaker = Matchmaker(args.config_path, dry_run=not args.doit)

	matchmaker.clean()

	matches = matchmaker.find_matches()
	matchmaker.process_found_matches(matches)

	matchmaker.process_approved_matches()

if __name__ == '__main__':
	main()