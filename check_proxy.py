import redis
import gevent
import pickle
import logging
import requests
import pycountry
import subprocess
from gevent import monkey, pool
from jinja2 import Environment, FileSystemLoader

from parsers import get_all_proxies
from neutrinoapi import check_neutrinoapi

from settings import PROXY_CHECK_WORKERS, PROXY_CHECK_URL, PROXY_CHECK_TIMEOUT, REDIS_HOST, REDIS_PORT, REDIS_DB, \
    FIRST_LOCAL_PORT, SQUID_CONF_PATH, EXTERNAL_IP, SQUID3_PATH, MAX_PROXIES_IN_COUNTRY, EXTRA_COUNTRIES, LOGGING_LEVEL

monkey.patch_all()

jobs = []
PROXY_COUNT_BY_COUNTRY = {}
TMP_DATA = {'all_proxy_count': 0}
PROXY_COUNTRIES = {c.alpha_2: [] for c in pycountry.countries}
PROXY_COUNTRIES.update(EXTRA_COUNTRIES)

redis_conn = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)

env = Environment(loader=FileSystemLoader('templates'))


def proxy_check(proxy):
    proxy_addr = proxy['address']
    proxy_name = proxy['name']

    full_proxy_addr = 'http://{proxy_addr}'.format(proxy_addr=proxy_addr)
    try:
        proxy_host, proxy_port = proxy_addr.split(':')
    except:
        return None

    try:
        r = requests.get(PROXY_CHECK_URL, proxies={'http': full_proxy_addr}, timeout=PROXY_CHECK_TIMEOUT)
        assert r.status_code == 200
        real_ip, country_code = r.text.split(' ')
        response_time = r.elapsed.total_seconds()
    except Exception as e:
        logging.debug(
            'Error check proxy {proxy_addr}: {description}'.format(proxy_addr=full_proxy_addr, description=str(e)))
        return None

    if country_code not in PROXY_COUNTRIES.keys():
        logging.fatal('Unsupported country {country_code} detected'.format(country_code=country_code))
        return None

    if check_neutrinoapi(real_ip):
        return None

    if not PROXY_COUNTRIES.get(country_code):
        PROXY_COUNT_BY_COUNTRY[country_code] = 0

    result = {
        'povider': proxy_name,
        'real_ip': real_ip,
        'proxy_host': proxy_host,
        'proxy_port': proxy_port,
        'country_code': country_code,
        'response_time': response_time,
    }

    PROXY_COUNTRIES[country_code].append(result)

    PROXY_COUNT_BY_COUNTRY[country_code] += 1
    TMP_DATA['all_proxy_count'] += 1

    logging.debug('Working proxy found: %s' % proxy_addr)

    return result


def update_squid3_forward_conf():
    if not PROXY_COUNT_BY_COUNTRY:
        logging.error('Any new proxies found/checked. Abort squid reconfigure.')
        return False

    squid_conf = open(SQUID_CONF_PATH, 'w+')

    country_counter = 1
    for proxy_country in sorted(PROXY_COUNTRIES.keys()):
        squid_conf.write('# Country {proxy_country} configs: \n'.format(proxy_country=proxy_country))

        PROXY_COUNTRIES[proxy_country] = sorted(
            PROXY_COUNTRIES[proxy_country], key=lambda k: k['response_time'])[:MAX_PROXIES_IN_COUNTRY]

        proxy_counter = 1
        for proxy_info in PROXY_COUNTRIES[proxy_country]:
            template = env.get_template('forwarding.conf')

            proxy_info['connect_port'] = FIRST_LOCAL_PORT + country_counter * MAX_PROXIES_IN_COUNTRY + proxy_counter
            proxy_info['proxy_line'] = 'http://{external_ip}:{connect_port}'.format(
                external_ip=EXTERNAL_IP,
                connect_port=proxy_info['connect_port']
            )

            data = template.render(proxy_host=proxy_info['proxy_host'],
                                   proxy_port=proxy_info['proxy_port'],
                                   connect_port=proxy_info['connect_port'])
            squid_conf.write(data + '\n')
            proxy_counter += 1

        squid_conf.write('###\n')
        country_counter += 1

    squid_conf.close()

    subprocess.call([SQUID3_PATH, '-k', 'reconfigure'])

    logging.info('Squid3 conf updated')


def main():
    logging.basicConfig(level=LOGGING_LEVEL)

    p = pool.Pool(PROXY_CHECK_WORKERS)

    for proxy in get_all_proxies():
        jobs.append(p.spawn(proxy_check, proxy))

    gevent.joinall(jobs)

    update_squid3_forward_conf()

    redis_conn.set('proxy_countries', pickle.dumps(PROXY_COUNTRIES))
    redis_conn.set('proxy_count_by_country', pickle.dumps(PROXY_COUNT_BY_COUNTRY))
    redis_conn.set('all_proxy_count', pickle.dumps(TMP_DATA['all_proxy_count']))

    print('Done: %d proxies in %d countries saved to redis & squid conf' % (
    TMP_DATA['all_proxy_count'], len(PROXY_COUNT_BY_COUNTRY)))


if __name__ == '__main__':
    main()